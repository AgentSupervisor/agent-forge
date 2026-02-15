"""TelegramConnector — Telegram bot connector via python-telegram-bot."""

from __future__ import annotations

import logging
import re
import tempfile
from pathlib import Path
from typing import Any

from .base import ActionButton, BaseConnector, ConnectorType, InboundMessage, OutboundMessage

logger = logging.getLogger(__name__)


class TelegramConnector(BaseConnector):
    """Telegram bot connector using python-telegram-bot polling."""

    connector_type = ConnectorType.TELEGRAM

    def __init__(self, connector_id: str, config: dict[str, Any]) -> None:
        super().__init__(connector_id, config)
        self.bot_token: str = config.get("credentials", {}).get("bot_token", "")
        self.allowed_users: list[int] = config.get("settings", {}).get(
            "allowed_users", []
        )
        self._app: Any = None
        self._bot: Any = None
        # Load persisted recent chats from settings, fall back to empty
        self._recent_chats: dict[str, dict[str, str]] = config.get("settings", {}).get(
            "known_chats", {}
        )

    async def start(self) -> None:
        from telegram import Update
        from telegram.ext import (
            Application,
            CallbackQueryHandler,
            CommandHandler,
            MessageHandler,
            filters,
        )

        self._app = Application.builder().token(self.bot_token).build()

        # Commands
        for cmd in ("status", "spawn", "kill", "projects",
                     "approve", "reject", "interrupt", "approve_all",
                     "help", "commands", "start"):
            self._app.add_handler(CommandHandler(cmd, self._handle_command))

        # Inline button callbacks
        self._app.add_handler(CallbackQueryHandler(self._handle_callback))

        # Text messages
        self._app.add_handler(
            MessageHandler(
                filters.TEXT & ~filters.COMMAND,
                self._handle_text,
            )
        )

        # Media messages
        self._app.add_handler(
            MessageHandler(
                filters.PHOTO
                | filters.VIDEO
                | filters.AUDIO
                | filters.Document.ALL
                | filters.VOICE,
                self._handle_media,
            )
        )

        await self._app.initialize()
        self._bot = self._app.bot

        # Seed recent chats from pending updates before polling consumes them
        try:
            updates = await self._bot.get_updates(limit=100, timeout=1)
            if not updates:
                # No pending updates — try negative offset to grab recent ones
                # from the server queue (works if updates are <24h old)
                updates = await self._bot.get_updates(offset=-100, limit=100, timeout=1)
            for upd in updates:
                chat = upd.effective_chat
                if chat:
                    self._track_chat(chat)
            if updates:
                # Confirm seeded updates so the updater doesn't reprocess them
                max_id = max(u.update_id for u in updates)
                await self._bot.get_updates(offset=max_id + 1, timeout=0)
                logger.info(
                    "Seeded %d chat(s) from %d update(s) (confirmed up to %d)",
                    len(self._recent_chats), len(updates), max_id,
                )
        except Exception:
            logger.debug("Could not seed chats from getUpdates", exc_info=True)

        await self._app.start()
        await self._app.updater.start_polling()
        self._running = True
        logger.info("TelegramConnector '%s' started", self.connector_id)

    async def stop(self) -> None:
        if self._app:
            try:
                await self._app.updater.stop()
                await self._app.stop()
                await self._app.shutdown()
            except Exception:
                logger.exception("Error stopping TelegramConnector '%s'", self.connector_id)
        self._running = False
        self._bot = None
        logger.info("TelegramConnector '%s' stopped", self.connector_id)

    async def send_message(self, message: OutboundMessage) -> bool:
        if not self._bot:
            return False
        try:
            # Build inline keyboard from action buttons if present
            reply_markup = None
            buttons: list[ActionButton] = message.extra.get("action_buttons", [])
            if buttons:
                from telegram import InlineKeyboardButton, InlineKeyboardMarkup

                keyboard = [
                    InlineKeyboardButton(
                        btn.label,
                        callback_data=f"ctrl:{btn.agent_id}:{btn.action}",
                    )
                    for btn in buttons
                ]
                reply_markup = InlineKeyboardMarkup([keyboard])

            await self._bot.send_message(
                chat_id=message.channel_id,
                text=message.text,
                parse_mode=message.parse_mode or None,
                reply_markup=reply_markup,
            )
            for path in message.media_paths:
                with open(path, "rb") as f:
                    await self._bot.send_document(chat_id=message.channel_id, document=f)
            return True
        except Exception:
            logger.exception("Failed to send Telegram message to %s", message.channel_id)
            return False

    async def validate_channel(self, channel_id: str) -> bool:
        if not self._bot:
            return False
        try:
            await self._bot.get_chat(chat_id=channel_id)
            return True
        except Exception:
            return False

    async def get_channel_info(self, channel_id: str) -> dict[str, Any]:
        if not self._bot:
            return {}
        try:
            chat = await self._bot.get_chat(chat_id=channel_id)
            name = chat.title or " ".join(
                filter(None, [chat.first_name, chat.last_name])
            ) or chat.username or str(chat.id)
            return {
                "id": str(chat.id),
                "name": name,
                "type": chat.type,
                "username": chat.username or "",
            }
        except Exception:
            return {}

    async def list_channels(self) -> list[dict[str, str]]:
        # Return recently seen chats (Telegram bots cannot enumerate chats)
        return [
            {"id": chat_id, "name": info.get("name", chat_id), "type": info.get("type", "")}
            for chat_id, info in self._recent_chats.items()
        ]

    def get_known_chats(self) -> dict[str, dict[str, str]]:
        """Return recent chats dict for persistence (plain strings only)."""
        return {
            cid: {k: str(v) for k, v in info.items()}
            for cid, info in self._recent_chats.items()
        }

    def _track_chat(self, chat: Any) -> None:
        """Record a chat in the recent chats map."""
        chat_id = str(chat.id)
        name = chat.title or " ".join(
            filter(None, [getattr(chat, "first_name", ""), getattr(chat, "last_name", "")])
        ) or getattr(chat, "username", "") or chat_id
        is_new = chat_id not in self._recent_chats
        self._recent_chats[chat_id] = {"name": name, "type": str(chat.type)}
        if is_new:
            logger.info("Tracked new chat: id=%s name='%s' type=%s", chat_id, name, chat.type)

    async def health_check(self) -> dict[str, Any]:
        if not self._bot:
            return {"connected": False, "details": "Bot not started"}
        try:
            me = await self._bot.get_me()
            return {
                "connected": True,
                "bot_username": me.username,
                "bot_id": me.id,
            }
        except Exception as exc:
            return {"connected": False, "details": str(exc)}

    # ------------------------------------------------------------------
    # Internal handlers
    # ------------------------------------------------------------------

    def _check_authorized(self, user_id: int) -> bool:
        if not self.allowed_users:
            return True
        return user_id in self.allowed_users

    async def _handle_command(self, update: Any, context: Any) -> None:
        if not self._check_authorized(update.effective_user.id):
            await update.message.reply_text("Not authorized.")
            return

        self._track_chat(update.effective_chat)
        text = update.message.text or ""
        parts = text.split()
        command_name = parts[0].lstrip("/").split("@")[0]  # strip /cmd@botname
        command_args = parts[1:]

        msg = InboundMessage(
            connector_id=self.connector_id,
            channel_id=str(update.effective_chat.id),
            sender_id=str(update.effective_user.id),
            sender_name=update.effective_user.first_name or "",
            is_command=True,
            command_name=command_name,
            command_args=command_args,
            raw=update,
        )

        if self._message_callback:
            await self._message_callback(msg)

    async def _handle_text(self, update: Any, context: Any) -> None:
        if not self._check_authorized(update.effective_user.id):
            await update.message.reply_text("Not authorized.")
            return

        self._track_chat(update.effective_chat)
        text = update.message.text or ""
        project_name, agent_id = self._parse_routing(text)
        if project_name:
            # Strip the @project[:agent] prefix from the text
            match = re.match(r"^@[\w-]+(?::[\w-]+)?\s+(.*)", text, re.DOTALL)
            text = match.group(1).strip() if match else text

        msg = InboundMessage(
            connector_id=self.connector_id,
            channel_id=str(update.effective_chat.id),
            sender_id=str(update.effective_user.id),
            sender_name=update.effective_user.first_name or "",
            text=text,
            project_name=project_name,
            agent_id=agent_id,
            raw=update,
        )

        if self._message_callback:
            await self._message_callback(msg)

    async def _handle_media(self, update: Any, context: Any) -> None:
        if not self._check_authorized(update.effective_user.id):
            await update.message.reply_text("Not authorized.")
            return

        self._track_chat(update.effective_chat)
        caption = update.message.caption or ""
        project_name, agent_id = self._parse_routing(caption)
        if project_name:
            match = re.match(r"^@[\w-]+(?::[\w-]+)?\s+(.*)", caption, re.DOTALL)
            caption = match.group(1).strip() if match else caption

        # Download attachment
        media_paths: list[str] = []
        try:
            file_obj = None
            if update.message.photo:
                file_obj = await update.message.photo[-1].get_file()
            elif update.message.video:
                file_obj = await update.message.video.get_file()
            elif update.message.audio:
                file_obj = await update.message.audio.get_file()
            elif update.message.voice:
                file_obj = await update.message.voice.get_file()
            elif update.message.document:
                file_obj = await update.message.document.get_file()

            if file_obj:
                tmp_dir = tempfile.mkdtemp(prefix="forge_media_")
                file_name = (
                    Path(file_obj.file_path).name
                    if file_obj.file_path
                    else "attachment"
                )
                tmp_path = Path(tmp_dir) / file_name
                await file_obj.download_to_drive(str(tmp_path))
                media_paths.append(str(tmp_path))
        except Exception:
            logger.exception("Failed to download Telegram media")

        msg = InboundMessage(
            connector_id=self.connector_id,
            channel_id=str(update.effective_chat.id),
            sender_id=str(update.effective_user.id),
            sender_name=update.effective_user.first_name or "",
            text=caption,
            media_paths=media_paths,
            project_name=project_name,
            agent_id=agent_id,
            raw=update,
        )

        if self._message_callback:
            await self._message_callback(msg)

    async def _handle_callback(self, update: Any, context: Any) -> None:
        """Handle inline keyboard button presses (callback queries)."""
        query = update.callback_query
        if not query or not query.data:
            return

        if not self._check_authorized(update.effective_user.id):
            await query.answer("Not authorized.")
            return

        # Parse callback data: "ctrl:{agent_id}:{action}"
        parts = query.data.split(":", 2)
        if len(parts) != 3 or parts[0] != "ctrl":
            await query.answer("Invalid action.")
            return

        _, agent_id, action = parts

        msg = InboundMessage(
            connector_id=self.connector_id,
            channel_id=str(update.effective_chat.id),
            sender_id=str(update.effective_user.id),
            sender_name=update.effective_user.first_name or "",
            is_command=True,
            command_name=action,
            command_args=[agent_id],
            raw=update,
        )

        if self._message_callback:
            await self._message_callback(msg)

        await query.answer(f"{action} sent")

    @staticmethod
    def _parse_routing(text: str) -> tuple[str, str]:
        """Extract @project[:agent_id] from text. Returns (project, agent_id)."""
        match = re.match(r"^@([\w-]+)(?::([\w-]+))?\s", text)
        if not match:
            return "", ""
        return match.group(1), match.group(2) or ""
