"""Downloads and processes media files, stages them in agent worktrees."""

from __future__ import annotations

import asyncio
import logging
import shutil
import time
from enum import Enum
from pathlib import Path

logger = logging.getLogger(__name__)

class MediaType(str, Enum):
    IMAGE = "image"
    VIDEO = "video"
    AUDIO = "audio"
    DOCUMENT = "document"

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tiff"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".webm", ".avi", ".mkv"}
AUDIO_EXTENSIONS = {".ogg", ".mp3", ".wav", ".m4a", ".flac", ".opus"}
DOCUMENT_EXTENSIONS = {".pdf", ".docx", ".doc", ".txt", ".csv", ".xlsx", ".md", ".json"}


class MediaHandler:
    """Downloads and processes media files, stages them in agent worktrees."""

    def __init__(self, temp_dir: str = "/tmp/agent-forge-media"):
        self.temp_dir = Path(temp_dir)
        self.temp_dir.mkdir(parents=True, exist_ok=True)

    async def process_and_stage(
        self,
        source_path: str,
        agent_worktree: str,
        media_type: MediaType | None = None,
    ) -> tuple[list[str], MediaType]:
        """Process media and stage in worktree.

        Returns (staged_paths, detected_media_type) so callers can use
        build_media_reference().
        """
        if media_type is None:
            media_type = self._detect_type(source_path)
        media_dir = self._ensure_media_dir(agent_worktree)
        timestamp = int(time.time())
        source = Path(source_path)
        staged_paths: list[str] = []

        if media_type == MediaType.IMAGE:
            # Copy image, resize if too large
            resized = await self._resize_image(source_path)
            dest_name = f"{timestamp}_{source.name}"
            dest = media_dir / dest_name
            shutil.copy2(resized, dest)
            staged_paths.append(f".media/{dest_name}")

        elif media_type == MediaType.VIDEO:
            # Stage original video file
            dest_name = f"{timestamp}_{source.name}"
            dest = media_dir / dest_name
            shutil.copy2(source_path, dest)
            staged_paths.append(f".media/{dest_name}")
            # Also extract keyframes for visual context
            frame_dir = self.temp_dir / f"frames_{timestamp}"
            frame_dir.mkdir(exist_ok=True)
            frames = await self._extract_video_frames(source_path, str(frame_dir))
            for frame_path in frames:
                frame_file = Path(frame_path)
                fname = f"{timestamp}_{frame_file.name}"
                fdest = media_dir / fname
                shutil.copy2(frame_path, fdest)
                staged_paths.append(f".media/{fname}")

        elif media_type == MediaType.AUDIO:
            # Transcribe or save as-is
            transcript = await self._transcribe_audio(source_path, str(self.temp_dir))
            if transcript:
                # Save transcript as text file
                txt_name = f"{timestamp}_transcript.txt"
                txt_dest = media_dir / txt_name
                txt_dest.write_text(transcript)
                staged_paths.append(f".media/{txt_name}")
            # Also save original audio
            dest_name = f"{timestamp}_{source.name}"
            dest = media_dir / dest_name
            shutil.copy2(source_path, dest)
            staged_paths.append(f".media/{dest_name}")

        elif media_type == MediaType.DOCUMENT:
            # Copy as-is
            dest_name = f"{timestamp}_{source.name}"
            dest = media_dir / dest_name
            shutil.copy2(source_path, dest)
            staged_paths.append(f".media/{dest_name}")

        return staged_paths, media_type

    def _ensure_media_dir(self, worktree: str) -> Path:
        media_dir = Path(worktree) / ".media"
        media_dir.mkdir(exist_ok=True)
        return media_dir

    def _detect_type(self, filename: str) -> MediaType:
        ext = Path(filename).suffix.lower()
        if ext in IMAGE_EXTENSIONS:
            return MediaType.IMAGE
        elif ext in VIDEO_EXTENSIONS:
            return MediaType.VIDEO
        elif ext in AUDIO_EXTENSIONS:
            return MediaType.AUDIO
        else:
            return MediaType.DOCUMENT

    async def _extract_video_frames(
        self, video_path: str, output_dir: str
    ) -> list[str]:
        """Extract keyframes from video using ffmpeg."""
        # First get duration
        duration = await self._get_video_duration(video_path)

        if duration is not None and duration < 10:
            # Short video: extract one frame per second
            cmd = [
                "ffmpeg", "-i", video_path,
                "-vf", "fps=1",
                "-frames:v", "10",
                f"{output_dir}/frame_%03d.png",
            ]
        else:
            # Extract keyframes
            cmd = [
                "ffmpeg", "-i", video_path,
                "-vf", "select='eq(ptype,I)'",
                "-vsync", "vfr",
                "-frames:v", "10",
                f"{output_dir}/frame_%03d.png",
            ]

        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        try:
            await asyncio.wait_for(proc.communicate(), timeout=60)
        except asyncio.TimeoutError:
            proc.kill()
            logger.error("ffmpeg timed out extracting frames from %s", video_path)
            return []

        # Return sorted list of extracted frames
        frames = sorted(Path(output_dir).glob("frame_*.png"))
        return [str(f) for f in frames]

    async def _get_video_duration(self, video_path: str) -> float | None:
        cmd = [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            video_path,
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await proc.communicate()
        try:
            return float(stdout.decode().strip())
        except (ValueError, AttributeError):
            return None

    async def _transcribe_audio(
        self, audio_path: str, output_dir: str
    ) -> str | None:
        """Transcribe using whisper if available."""
        # Check if whisper is installed
        if not shutil.which("whisper"):
            logger.info("whisper not installed, skipping transcription")
            return None

        cmd = [
            "whisper", audio_path,
            "--model", "base",
            "--output_format", "txt",
            "--output_dir", output_dir,
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        try:
            await asyncio.wait_for(proc.communicate(), timeout=300)
        except asyncio.TimeoutError:
            proc.kill()
            logger.error("whisper timed out transcribing %s", audio_path)
            return None

        # Read the output txt file
        audio_name = Path(audio_path).stem
        txt_path = Path(output_dir) / f"{audio_name}.txt"
        if txt_path.exists():
            return txt_path.read_text().strip()
        return None

    async def _resize_image(
        self, image_path: str, max_dimension: int = 4000
    ) -> str:
        """Resize if image exceeds max_dimension. Returns path to (possibly resized) image."""
        # Use ffprobe to get dimensions
        cmd = [
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height",
            "-of", "csv=p=0",
            image_path,
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await proc.communicate()

        try:
            dims = stdout.decode().strip().split(",")
            width, height = int(dims[0]), int(dims[1])
        except (ValueError, IndexError):
            return image_path  # Can't determine size, return as-is

        if width <= max_dimension and height <= max_dimension:
            return image_path

        # Resize
        output_path = str(self.temp_dir / f"resized_{Path(image_path).name}")
        scale = f"'if(gt(iw,ih),{max_dimension},-2):if(gt(ih,iw),{max_dimension},-2)'"
        cmd = [
            "ffmpeg", "-i", image_path,
            "-vf", f"scale={scale}",
            "-y", output_path,
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        await asyncio.wait_for(proc.communicate(), timeout=60)

        return output_path if Path(output_path).exists() else image_path

    def build_media_reference(
        self, staged_paths: list[str], media_type: MediaType
    ) -> str:
        """Build text referencing staged media files."""
        if not staged_paths:
            return ""

        paths_str = ", ".join(staged_paths)

        if media_type == MediaType.IMAGE:
            return f"I've placed design mockups/images at: {paths_str}. Please analyze them."
        elif media_type == MediaType.VIDEO:
            video_files = [p for p in staged_paths if Path(p).suffix.lower() in VIDEO_EXTENSIONS]
            frame_files = [p for p in staged_paths if p not in video_files]
            parts = []
            if video_files:
                parts.append(f"Video file at: {', '.join(video_files)}")
            if frame_files:
                parts.append(f"Extracted keyframes at: {', '.join(frame_files)}")
            return ". ".join(parts) + "."
        elif media_type == MediaType.AUDIO:
            # Check if there's a transcript
            transcripts = [p for p in staged_paths if "transcript" in p]
            audio_files = [p for p in staged_paths if "transcript" not in p]
            parts = []
            if transcripts:
                parts.append(
                    f"Voice message transcript is at: {', '.join(transcripts)}"
                )
            if audio_files:
                parts.append(
                    f"Original audio file at: {', '.join(audio_files)}"
                )
            return ". ".join(parts)
        elif media_type == MediaType.DOCUMENT:
            return f"I've placed the document(s) at: {paths_str}. Please review."
        return ""
