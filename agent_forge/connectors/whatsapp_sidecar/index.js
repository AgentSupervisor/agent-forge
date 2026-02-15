#!/usr/bin/env node

const { default: makeWASocket, useMultiFileAuthState, DisconnectReason, fetchLatestBaileysVersion, makeInMemoryStore, downloadMediaMessage } = require('@whiskeysockets/baileys');
const express = require('express');
const multer = require('multer');
const QRCode = require('qrcode');
const qrcodeTerminal = require('qrcode-terminal');
const pino = require('pino');
const fs = require('fs');
const path = require('path');
const os = require('os');

// Parse CLI arguments
const args = process.argv.slice(2);
let port = 3100;
let sessionDir = null;

for (let i = 0; i < args.length; i++) {
  if (args[i] === '--port' && i + 1 < args.length) {
    port = parseInt(args[i + 1], 10);
    i++;
  } else if (args[i] === '--session-dir' && i + 1 < args.length) {
    sessionDir = args[i + 1];
    i++;
  }
}

if (!sessionDir) {
  console.error('Error: --session-dir is required');
  process.exit(1);
}

// Ensure session directory exists
if (!fs.existsSync(sessionDir)) {
  fs.mkdirSync(sessionDir, { recursive: true });
}

// State
let sock = null;
let qrCode = null;
let isConnected = false;
let messageQueue = [];
const chatMap = new Map(); // jid -> {name, isGroup}
const logger = pino({ level: 'warn' });

// Express setup
const app = express();
app.use(express.json());

const upload = multer({ dest: os.tmpdir() });

// Health endpoint
app.get('/health', (req, res) => {
  res.json({
    connected: isConnected,
    qr_available: qrCode !== null && !isConnected
  });
});

// QR endpoint
app.get('/qr', (req, res) => {
  if (qrCode && !isConnected) {
    QRCode.toDataURL(qrCode, (err, url) => {
      if (err) {
        res.status(500).json({ error: 'Failed to generate QR code' });
      } else {
        res.json({
          qr_text: qrCode,
          qr_base64: url
        });
      }
    });
  } else {
    res.status(404).json({ error: 'No QR code available' });
  }
});

// Send text message
app.post('/send', async (req, res) => {
  try {
    const { jid, text, buttons } = req.body;

    if (!jid || !text) {
      return res.status(400).json({ error: 'jid and text are required' });
    }

    if (!isConnected || !sock) {
      return res.status(503).json({ error: 'WhatsApp not connected' });
    }

    let message;

    if (buttons && buttons.length > 0) {
      // WhatsApp has deprecated buttons in most regions, so we send both formats
      const maxButtons = Math.min(buttons.length, 3);
      const buttonList = buttons.slice(0, maxButtons);

      // Send as text with button options
      const buttonText = buttonList.map((b, i) => `${i + 1}. ${b.text}`).join('\n');
      const fullText = `${text}\n\n${buttonText}`;

      try {
        // Try to send as button message (may not work in all regions)
        const buttonMessage = {
          text: text,
          footer: '',
          buttons: buttonList.map(b => ({ buttonId: b.id, buttonText: { displayText: b.text }, type: 1 })),
          headerType: 1
        };
        message = await sock.sendMessage(jid, buttonMessage);
      } catch (buttonErr) {
        // Fallback to plain text with numbered options
        message = await sock.sendMessage(jid, { text: fullText });
      }
    } else {
      message = await sock.sendMessage(jid, { text });
    }

    res.json({ success: true, id: message.key.id });
  } catch (error) {
    console.error('Send error:', error);
    res.status(500).json({ error: error.message });
  }
});

// Send media
app.post('/send_media', upload.single('file'), async (req, res) => {
  try {
    const { jid } = req.body;
    const file = req.file;

    if (!jid || !file) {
      return res.status(400).json({ error: 'jid and file are required' });
    }

    if (!isConnected || !sock) {
      return res.status(503).json({ error: 'WhatsApp not connected' });
    }

    const mimetype = file.mimetype || 'application/octet-stream';
    const fileBuffer = fs.readFileSync(file.path);

    let messageContent;
    if (mimetype.startsWith('image/')) {
      messageContent = { image: fileBuffer, mimetype, fileName: file.originalname };
    } else if (mimetype.startsWith('video/')) {
      messageContent = { video: fileBuffer, mimetype, fileName: file.originalname };
    } else if (mimetype.startsWith('audio/')) {
      messageContent = { audio: fileBuffer, mimetype, fileName: file.originalname };
    } else {
      messageContent = { document: fileBuffer, mimetype, fileName: file.originalname };
    }

    const message = await sock.sendMessage(jid, messageContent);

    // Clean up uploaded file
    fs.unlinkSync(file.path);

    res.json({ success: true, id: message.key.id });
  } catch (error) {
    console.error('Send media error:', error);
    if (req.file && req.file.path) {
      fs.unlinkSync(req.file.path);
    }
    res.status(500).json({ error: error.message });
  }
});

// Get messages
app.get('/messages', (req, res) => {
  const messages = [...messageQueue];
  messageQueue = [];
  res.json(messages);
});

// Get chats
app.get('/chats', (req, res) => {
  const chats = Array.from(chatMap.entries()).map(([jid, info]) => ({
    jid,
    name: info.name,
    isGroup: info.isGroup
  }));
  res.json({ chats });
});

// Get specific chat
app.get('/chat/:jid', (req, res) => {
  const { jid } = req.params;
  const chat = chatMap.get(jid);

  if (chat) {
    res.json({ jid, name: chat.name, isGroup: chat.isGroup });
  } else {
    res.status(404).json({ error: 'Chat not found' });
  }
});

// Shutdown
app.post('/shutdown', async (req, res) => {
  res.json({ success: true });

  try {
    if (sock) {
      sock.end(undefined);
    }
  } catch (error) {
    console.error('Logout error:', error);
  }

  server.close(() => {
    process.exit(0);
  });
});

// Start Baileys
async function startBaileys() {
  const { state, saveCreds } = await useMultiFileAuthState(sessionDir);
  const { version } = await fetchLatestBaileysVersion();

  const store = makeInMemoryStore({ logger });

  sock = makeWASocket({
    version,
    auth: state,
    logger,
    printQRInTerminal: false,
    generateHighQualityLinkPreview: true
  });

  store.bind(sock.ev);

  // Connection updates
  sock.ev.on('connection.update', (update) => {
    const { connection, lastDisconnect, qr } = update;

    if (qr) {
      qrCode = qr;
      console.log('QR Code received - scan with WhatsApp');
      qrcodeTerminal.generate(qr, { small: true });
    }

    if (connection === 'close') {
      const shouldReconnect = lastDisconnect?.error?.output?.statusCode !== DisconnectReason.loggedOut;
      console.log('Connection closed. Reconnect:', shouldReconnect);

      isConnected = false;
      qrCode = null;

      if (shouldReconnect) {
        setTimeout(startBaileys, 3000);
      }
    } else if (connection === 'open') {
      console.log('WhatsApp connected!');
      isConnected = true;
      qrCode = null;
    }
  });

  // Credentials update
  sock.ev.on('creds.update', saveCreds);

  // Message handling
  sock.ev.on('messages.upsert', async ({ messages, type }) => {
    for (const msg of messages) {
      // Skip messages from self
      if (msg.key.fromMe) continue;

      const remoteJid = msg.key.remoteJid;
      const participant = msg.key.participant;
      const isGroup = remoteJid.endsWith('@g.us');
      const chatJid = remoteJid;
      const from = isGroup ? (participant || remoteJid) : remoteJid;
      const pushName = msg.pushName || 'Unknown';

      // Extract text content
      let text = msg.message?.conversation ||
                 msg.message?.extendedTextMessage?.text ||
                 msg.message?.imageMessage?.caption ||
                 msg.message?.videoMessage?.caption ||
                 '';

      // Handle button responses
      let selectedButtonId = null;
      if (msg.message?.buttonsResponseMessage) {
        selectedButtonId = msg.message.buttonsResponseMessage.selectedButtonId;
        text = msg.message.buttonsResponseMessage.selectedDisplayText || selectedButtonId;
      } else if (msg.message?.listResponseMessage) {
        selectedButtonId = msg.message.listResponseMessage.singleSelectReply?.selectedRowId;
        text = msg.message.listResponseMessage.title || selectedButtonId;
      }

      // Track chat
      chatMap.set(chatJid, { name: pushName, isGroup });
      if (isGroup && participant) {
        chatMap.set(participant, { name: pushName, isGroup: false });
      }

      // Handle media
      let media = null;
      const messageType = Object.keys(msg.message || {})[0];
      const mediaTypes = ['imageMessage', 'videoMessage', 'audioMessage', 'documentMessage'];

      if (mediaTypes.includes(messageType)) {
        try {
          const buffer = await downloadMediaMessage(msg, 'buffer', {});
          const mediaMsg = msg.message[messageType];
          const mimetype = mediaMsg.mimetype || 'application/octet-stream';
          const ext = mimetype.split('/')[1]?.split(';')[0] || 'bin';
          const filename = mediaMsg.fileName || `media_${Date.now()}.${ext}`;

          const tempPath = path.join(os.tmpdir(), `whatsapp_${Date.now()}_${filename}`);
          fs.writeFileSync(tempPath, buffer);

          media = {
            mimetype,
            path: tempPath,
            filename
          };
        } catch (error) {
          console.error('Media download error:', error);
        }
      }

      // Queue message
      const queuedMessage = {
        id: msg.key.id,
        from,
        pushName,
        text,
        timestamp: msg.messageTimestamp,
        media,
        isGroup,
        chatJid
      };

      if (selectedButtonId) {
        queuedMessage.selectedButtonId = selectedButtonId;
      }

      messageQueue.push(queuedMessage);
    }
  });
}

// Start server
const server = app.listen(port, () => {
  console.log(`WhatsApp sidecar listening on port ${port}`);
  startBaileys().catch(console.error);
});
