/**
 * AgentTerminal — xterm.js integration for Agent Forge.
 *
 * Connects to /ws/terminal/{agent_id} for real-time bidirectional I/O.
 * Uses xterm.js for terminal emulation, FitAddon for auto-resize,
 * and WebGL renderer for performance.
 */
class AgentTerminal {
    constructor(containerId, agentId) {
        this.containerId = containerId;
        this.agentId = agentId;
        this.ws = null;
        this.terminal = null;
        this.fitAddon = null;
        this.webglAddon = null;
        this._reconnectDelay = 1000;
        this._maxReconnectDelay = 30000;
        this._reconnectTimer = null;
        this._disposed = false;
    }

    connect() {
        // Create terminal
        this.terminal = new Terminal({
            cursorBlink: true,
            cursorStyle: 'bar',
            fontSize: 12,
            fontFamily: "'JetBrains Mono', 'SF Mono', 'Fira Code', monospace",
            lineHeight: 1.3,
            theme: {
                background: '#080a0e',
                foreground: '#e2e8f0',
                cursor: '#6366f1',
                cursorAccent: '#080a0e',
                selectionBackground: 'rgba(99, 102, 241, 0.3)',
                black: '#1a1e26',
                red: '#f87171',
                green: '#34d399',
                yellow: '#fbbf24',
                blue: '#60a5fa',
                magenta: '#c084fc',
                cyan: '#22d3ee',
                white: '#e2e8f0',
                brightBlack: '#5a6373',
                brightRed: '#fca5a5',
                brightGreen: '#6ee7b7',
                brightYellow: '#fde68a',
                brightBlue: '#93c5fd',
                brightMagenta: '#d8b4fe',
                brightCyan: '#67e8f9',
                brightWhite: '#f8fafc',
            },
            scrollback: 10000,
            allowProposedApi: true,
        });

        // Load FitAddon
        this.fitAddon = new FitAddon.FitAddon();
        this.terminal.loadAddon(this.fitAddon);

        // Open terminal in container
        const container = document.getElementById(this.containerId);
        this.terminal.open(container);

        // Try WebGL renderer for better performance
        try {
            this.webglAddon = new WebglAddon.WebglAddon();
            this.webglAddon.onContextLoss(() => {
                this.webglAddon.dispose();
                this.webglAddon = null;
            });
            this.terminal.loadAddon(this.webglAddon);
        } catch (e) {
            console.warn('[terminal] WebGL renderer not available, using canvas');
        }

        // Fit to container
        this.fitAddon.fit();

        // Handle window resize
        this._resizeObserver = new ResizeObserver(() => {
            if (this.fitAddon) {
                this.fitAddon.fit();
            }
        });
        this._resizeObserver.observe(container);

        // Handle terminal resize (fit addon changes cols/rows)
        this.terminal.onResize(({ cols, rows }) => {
            if (this.ws && this.ws.readyState === WebSocket.OPEN) {
                this.ws.send(JSON.stringify({ type: 'resize', cols, rows }));
            }
        });

        // Handle keyboard input: forward to WebSocket as binary
        this.terminal.onData((data) => {
            if (this.ws && this.ws.readyState === WebSocket.OPEN) {
                this.ws.send(new TextEncoder().encode(data));
            }
        });

        // Connect WebSocket
        this._connectWs();

        // Focus terminal
        this.terminal.focus();
    }

    _connectWs() {
        if (this._disposed) return;

        const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
        const url = proto + '//' + location.host + '/ws/terminal/' + this.agentId;

        this.ws = new WebSocket(url);
        this.ws.binaryType = 'arraybuffer';

        this.ws.onopen = () => {
            console.log('[terminal] WebSocket connected');
            this._reconnectDelay = 1000;

            // Send initial resize so server knows our dimensions
            if (this.terminal) {
                const { cols, rows } = this.terminal;
                this.ws.send(JSON.stringify({ type: 'resize', cols, rows }));
            }
        };

        this.ws.onmessage = (event) => {
            if (event.data instanceof ArrayBuffer) {
                // Binary frame: terminal output
                this.terminal.write(new Uint8Array(event.data));
            } else if (typeof event.data === 'string') {
                // Text frame: control message (future use)
                try {
                    const msg = JSON.parse(event.data);
                    // Handle control messages if needed
                } catch (e) {
                    // Not JSON, write as terminal data
                    this.terminal.write(event.data);
                }
            }
        };

        this.ws.onclose = () => {
            console.log('[terminal] WebSocket closed, reconnecting in ' + this._reconnectDelay + 'ms');
            this._scheduleReconnect();
        };

        this.ws.onerror = () => {};
    }

    _scheduleReconnect() {
        if (this._disposed) return;

        clearTimeout(this._reconnectTimer);
        this._reconnectTimer = setTimeout(() => {
            this._reconnectDelay = Math.min(this._reconnectDelay * 2, this._maxReconnectDelay);

            // Clear terminal on reconnect and show message
            if (this.terminal) {
                this.terminal.write('\r\n\x1b[33m[Reconnecting...]\x1b[0m\r\n');
            }

            this._connectWs();
        }, this._reconnectDelay);
    }

    dispose() {
        this._disposed = true;
        clearTimeout(this._reconnectTimer);
        if (this.ws) {
            this.ws.close();
            this.ws = null;
        }
        if (this._resizeObserver) {
            this._resizeObserver.disconnect();
        }
        if (this.webglAddon) {
            this.webglAddon.dispose();
        }
        if (this.fitAddon) {
            this.fitAddon.dispose();
        }
        if (this.terminal) {
            this.terminal.dispose();
        }
    }
}
