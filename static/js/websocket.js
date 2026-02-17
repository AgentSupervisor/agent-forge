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

    // Track previous statuses for change detection
    const previousStatuses = {};

    // Request notification permission on load
    if ("Notification" in window && Notification.permission === "default") {
        Notification.requestPermission();
    }

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
            } else if (data.type === "metrics_update") {
                handleMetricsUpdate(data);
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

    function maybeDesktopNotify(data) {
        if (!("Notification" in window) || Notification.permission !== "granted") return;
        if (document.visibilityState === "visible") return;

        var aid = data.agent_id;
        var status = data.status;
        var prev = previousStatuses[aid];

        // Same condition as IM connector: notify on state change unless new state is "working"
        if (prev === undefined || prev === status || status === "working") return;

        var label = (status || "idle").replace("_", " ");
        var project = data.project || "";
        var title = "Agent " + aid + (project ? " (" + project + ")" : "");
        var body = prev.replace("_", " ") + " → " + label;
        if (status === "waiting_input") {
            body = "Waiting for input";
            if (data.task) body += ": " + data.task;
        }

        var n = new Notification(title, { body: body, tag: "agent-" + aid });
        n.onclick = function () {
            window.focus();
            window.location.href = "/agents/" + aid;
            n.close();
        };
    }

    function handleAgentUpdate(data) {
        var aid = data.agent_id;
        var status = data.status;

        // Desktop notification on state change (before updating tracked status)
        maybeDesktopNotify(data);
        previousStatuses[aid] = status;

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

        // Update attention glow
        if (card) {
            if (data.needs_attention) {
                card.classList.add("attention");
            } else {
                card.classList.remove("attention");
            }
            if (data.parked) {
                card.classList.add("parked");
            } else {
                card.classList.remove("parked");
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

    function handleMetricsUpdate(data) {
        var sys = data.system || {};
        var agents = data.agents || {};

        // Helper to get color class based on percentage
        function getColorClass(percent) {
            if (percent === null || percent === undefined) return "low";
            if (percent >= 80) return "high";
            if (percent >= 50) return "medium";
            return "low";
        }

        // --- Dashboard stats bar: system metrics ---

        // CPU
        var cpuVal = document.getElementById("stat-cpu-val");
        var cpuFill = document.getElementById("stat-cpu-fill");
        if (cpuVal && sys.cpu_percent !== undefined && sys.cpu_percent !== null) {
            cpuVal.textContent = sys.cpu_percent.toFixed(1) + "%";
            if (cpuFill) {
                cpuFill.style.width = sys.cpu_percent + "%";
                cpuFill.className = "metric-gauge-fill " + getColorClass(sys.cpu_percent);
            }
        }

        // Memory
        var memVal = document.getElementById("stat-mem-val");
        var memFill = document.getElementById("stat-mem-fill");
        if (memVal && sys.memory_percent !== undefined && sys.memory_percent !== null) {
            memVal.textContent = sys.memory_percent.toFixed(1) + "%";
            if (memFill) {
                memFill.style.width = sys.memory_percent + "%";
                memFill.className = "metric-gauge-fill " + getColorClass(sys.memory_percent);
            }
        }

        // GPU (show/hide based on availability)
        var gpuItem = document.getElementById("stat-gpu-item");
        var gpuVal = document.getElementById("stat-gpu-val");
        var gpuFill = document.getElementById("stat-gpu-fill");
        if (gpuItem) {
            if (sys.gpu_utilization !== null && sys.gpu_utilization !== undefined) {
                gpuItem.style.display = "flex";
                if (gpuVal) gpuVal.textContent = sys.gpu_utilization.toFixed(1) + "%";
                if (gpuFill) {
                    gpuFill.style.width = sys.gpu_utilization + "%";
                    gpuFill.className = "metric-gauge-fill " + getColorClass(sys.gpu_utilization);
                }
            } else {
                gpuItem.style.display = "none";
            }
        }

        // Disk
        var diskVal = document.getElementById("stat-disk-val");
        if (diskVal && sys.disk_percent !== undefined && sys.disk_percent !== null) {
            diskVal.textContent = sys.disk_percent.toFixed(1) + "%";
        }

        // Network
        var netVal = document.getElementById("stat-net-val");
        if (netVal && sys.network_sent_mbps !== undefined && sys.network_recv_mbps !== undefined) {
            var sent = sys.network_sent_mbps !== null ? sys.network_sent_mbps.toFixed(1) : "0.0";
            var recv = sys.network_recv_mbps !== null ? sys.network_recv_mbps.toFixed(1) : "0.0";
            netVal.textContent = "\u2191" + sent + " \u2193" + recv;
        }

        // --- Dashboard: per-agent metrics ---

        for (var aid in agents) {
            var agentData = agents[aid];
            var cpuEl = document.getElementById("agent-cpu-" + aid);
            var memEl = document.getElementById("agent-mem-" + aid);

            if (cpuEl && agentData.cpu_percent !== undefined && agentData.cpu_percent !== null) {
                cpuEl.style.display = "flex";
                var cpuSpan = cpuEl.querySelector(".agent-cpu-val");
                if (cpuSpan) cpuSpan.textContent = agentData.cpu_percent.toFixed(1) + "%";
            }

            if (memEl && agentData.memory_mb !== undefined && agentData.memory_mb !== null) {
                memEl.style.display = "flex";
                var memSpan = memEl.querySelector(".agent-mem-val");
                if (memSpan) memSpan.textContent = agentData.memory_mb.toFixed(0) + " MB";
            }
        }

        // --- Agent detail page: agent metrics ---

        if (window.currentAgentId && agents[window.currentAgentId]) {
            var agentMetrics = agents[window.currentAgentId];

            var detailCpu = document.getElementById("detail-metric-cpu");
            if (detailCpu && agentMetrics.cpu_percent !== undefined && agentMetrics.cpu_percent !== null) {
                detailCpu.textContent = agentMetrics.cpu_percent.toFixed(1) + "%";
            }

            var detailMem = document.getElementById("detail-metric-mem");
            if (detailMem && agentMetrics.memory_mb !== undefined && agentMetrics.memory_mb !== null) {
                detailMem.textContent = agentMetrics.memory_mb.toFixed(1) + " MB";
            }

            var detailProcs = document.getElementById("detail-metric-procs");
            if (detailProcs && agentMetrics.process_count !== undefined && agentMetrics.process_count !== null) {
                detailProcs.textContent = agentMetrics.process_count;
            }
        }

        // --- Agent detail page: system metrics ---

        var detailSysCpu = document.getElementById("detail-sys-cpu");
        if (detailSysCpu && sys.cpu_percent !== undefined && sys.cpu_percent !== null) {
            detailSysCpu.textContent = sys.cpu_percent.toFixed(1) + "%";
        }

        var detailSysMem = document.getElementById("detail-sys-mem");
        if (detailSysMem && sys.memory_percent !== undefined && sys.memory_percent !== null) {
            var memText = sys.memory_percent.toFixed(1) + "%";
            if (sys.memory_used_mb !== undefined && sys.memory_total_mb !== undefined) {
                var usedGb = (sys.memory_used_mb / 1024).toFixed(1);
                var totalGb = (sys.memory_total_mb / 1024).toFixed(1);
                memText += " (" + usedGb + "/" + totalGb + " GB)";
            }
            detailSysMem.textContent = memText;
        }

        var detailSysGpuRow = document.getElementById("detail-sys-gpu-row");
        var detailSysGpu = document.getElementById("detail-sys-gpu");
        if (detailSysGpuRow && detailSysGpu) {
            if (sys.gpu_utilization !== null && sys.gpu_utilization !== undefined) {
                detailSysGpuRow.style.display = "flex";
                var gpuText = sys.gpu_utilization.toFixed(1) + "%";
                if (sys.gpu_memory_used_mb !== null && sys.gpu_memory_total_mb !== null &&
                    sys.gpu_memory_used_mb !== undefined && sys.gpu_memory_total_mb !== undefined) {
                    var gpuMemPct = (sys.gpu_memory_used_mb / sys.gpu_memory_total_mb * 100).toFixed(1);
                    gpuText += " (mem: " + gpuMemPct + "%)";
                }
                if (sys.gpu_temperature !== null && sys.gpu_temperature !== undefined) {
                    gpuText += " " + sys.gpu_temperature.toFixed(0) + "\u00B0C";
                }
                detailSysGpu.textContent = gpuText;
            } else {
                detailSysGpuRow.style.display = "none";
            }
        }

        // --- Metrics page: forward via custom event ---

        if (document.getElementById("metrics-page")) {
            window.dispatchEvent(new CustomEvent("metrics-update", { detail: data }));
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
