/**
 * Agent Forge WebSocket client.
 *
 * Connects to ws://{host}/ws.
 * Handles "agent_update" and "terminal_output" messages.
 * Auto-reconnects with exponential backoff (1s -> 30s max).
 */
(function () {
    "use strict";

    let ws = null;
    let reconnectDelay = 1000;
    const MAX_DELAY = 30000;

    function getWsUrl() {
        const proto = location.protocol === "https:" ? "wss:" : "ws:";
        return proto + "//" + location.host + "/ws";
    }

    function connect() {
        ws = new WebSocket(getWsUrl());

        ws.onopen = function () {
            console.log("[ws] connected");
            reconnectDelay = 1000;
        };

        ws.onmessage = function (event) {
            let data;
            try {
                data = JSON.parse(event.data);
            } catch (e) {
                return;
            }

            if (data.type === "agent_update") {
                handleAgentUpdate(data);
            } else if (data.type === "terminal_output") {
                handleTerminalOutput(data);
            }
        };

        ws.onclose = function () {
            console.log("[ws] closed, reconnecting in " + reconnectDelay + "ms");
            scheduleReconnect();
        };

        ws.onerror = function () {};
    }

    function scheduleReconnect() {
        setTimeout(function () {
            reconnectDelay = Math.min(reconnectDelay * 2, MAX_DELAY);
            connect();
        }, reconnectDelay);
    }

    // --- Status mappings ---

    const STATUS_DOT_CLASS = {
        working: "working",
        waiting_input: "waiting_input",
        idle: "idle",
        error: "error",
        stopped: "stopped",
        starting: "starting",
    };

    const STATUS_BADGE_CLASS = {
        working: "working",
        waiting_input: "waiting_input",
        idle: "idle",
        error: "error",
        stopped: "stopped",
        starting: "starting",
    };

    const STATUS_COLORS = {
        working: "var(--status-working)",
        waiting_input: "var(--status-waiting)",
        idle: "var(--status-idle)",
        error: "var(--status-error)",
        starting: "var(--status-starting)",
        stopped: "var(--status-stopped)",
    };

    function handleAgentUpdate(data) {
        var aid = data.agent_id;
        var status = data.status;

        // --- Dashboard card updates ---

        // Update pulse dot on card
        var dot = document.getElementById("dot-" + aid);
        if (dot) {
            var allStatuses = Object.values(STATUS_DOT_CLASS);
            allStatuses.forEach(function (c) { dot.classList.remove(c); });
            dot.classList.add(STATUS_DOT_CLASS[status] || "idle");
        }

        // Update status badge on card
        var badge = document.getElementById("badge-" + aid);
        if (badge) {
            var allBadges = Object.values(STATUS_BADGE_CLASS);
            allBadges.forEach(function (c) { badge.classList.remove(c); });
            badge.classList.add(STATUS_BADGE_CLASS[status] || "idle");
            badge.textContent = (status || "idle").replace("_", " ");
        }

        // Update card status bar color
        var card = document.getElementById("card-" + aid);
        if (card) {
            var bar = card.querySelector(".card-status-bar");
            if (bar) {
                bar.style.background = STATUS_COLORS[status] || STATUS_COLORS.idle;
            }
        }

        // Update sub-agent count on card
        if (data.sub_agent_count !== undefined) {
            var subEl = document.getElementById("sub-" + aid);
            if (subEl) {
                var span = subEl.querySelectorAll("span");
                if (span.length > 1) span[1].textContent = data.sub_agent_count + " sub";
            }
        }

        // --- Agent detail page updates ---

        if (window.currentAgentId === aid) {
            var detailDot = document.getElementById("detail-status-dot");
            if (detailDot) {
                var allS = Object.values(STATUS_DOT_CLASS);
                allS.forEach(function (c) { detailDot.classList.remove(c); });
                detailDot.classList.add(STATUS_DOT_CLASS[status] || "idle");
            }

            var detailBadge = document.getElementById("detail-status-badge");
            if (detailBadge) {
                var allB = Object.values(STATUS_BADGE_CLASS);
                allB.forEach(function (c) { detailBadge.classList.remove(c); });
                detailBadge.classList.add(STATUS_BADGE_CLASS[status] || "idle");
                detailBadge.textContent = (status || "idle").replace("_", " ");
            }

            // Update sub-agent count on detail page
            if (data.sub_agent_count !== undefined) {
                var detailSub = document.getElementById("detail-sub-count");
                if (detailSub) detailSub.textContent = data.sub_agent_count;
            }
        }
    }

    function handleTerminalOutput(data) {
        if (window.currentAgentId !== data.agent_id) return;

        var terminal = document.getElementById("terminal-view");
        if (!terminal) return;

        if (window._terminalAutoScroll === false) return;

        window._ignoreScrollUntil = Date.now() + 200;
        if (window._ansiUp) {
            terminal.innerHTML = window._ansiUp.ansi_to_html(data.output);
        } else {
            terminal.textContent = data.output;
        }
        if (window._terminalAutoScroll !== false) {
            terminal.scrollTop = terminal.scrollHeight;
        }
    }

    // Keep connection alive with periodic pings
    setInterval(function () {
        if (ws && ws.readyState === WebSocket.OPEN) {
            ws.send("ping");
        }
    }, 30000);

    // Start
    connect();
})();
