"""System and per-agent metrics collection using psutil and optional GPU monitoring."""

from __future__ import annotations

import logging
import os
import subprocess
import time
from typing import TYPE_CHECKING

import psutil
from pydantic import BaseModel

if TYPE_CHECKING:
    from .agent_manager import Agent, AgentManager

logger = logging.getLogger(__name__)

TMUX_TIMEOUT = 5


class SystemMetrics(BaseModel):
    """System-wide resource metrics."""
    cpu_percent: float
    memory_percent: float
    memory_used_mb: float
    memory_total_mb: float
    disk_percent: float
    disk_used_gb: float
    disk_total_gb: float
    load_avg_1min: float
    load_avg_5min: float
    load_avg_15min: float
    network_sent_mbps: float
    network_recv_mbps: float
    gpu_name: str | None = None
    gpu_utilization: float | None = None
    gpu_memory_used_mb: float | None = None
    gpu_memory_total_mb: float | None = None
    gpu_temperature: float | None = None


class AgentMetrics(BaseModel):
    """Per-agent resource metrics."""
    agent_id: str
    process_count: int
    cpu_percent: float
    memory_mb: float


class MetricsSnapshot(BaseModel):
    """Complete snapshot of system and agent metrics."""
    timestamp: float
    system: SystemMetrics
    agents: dict[str, AgentMetrics]
    total_agents_running: int
    total_agent_memory_mb: float


class MetricsCollector:
    """Collects system and per-agent metrics using psutil and pynvml (optional)."""

    def __init__(self, enable_gpu: bool = True) -> None:
        self.gpu_available = False
        self.gpu_handle = None
        self._last_net_io: tuple[float, float, float] | None = None  # (timestamp, bytes_sent, bytes_recv)
        # Cache psutil.Process objects keyed by PID so cpu_percent(interval=0)
        # has a prior measurement and returns meaningful values on the second call.
        self._proc_cache: dict[int, psutil.Process] = {}

        if enable_gpu:
            self._init_gpu()

        # Initialize network baseline
        try:
            net = psutil.net_io_counters()
            self._last_net_io = (time.time(), net.bytes_sent, net.bytes_recv)
        except Exception:
            logger.debug("Failed to initialize network baseline", exc_info=True)

    def _init_gpu(self) -> None:
        """Try to initialize pynvml for GPU monitoring."""
        try:
            import pynvml
            pynvml.nvmlInit()
            self.gpu_handle = pynvml.nvmlDeviceGetHandleByIndex(0)
            self.gpu_available = True
            logger.info("GPU monitoring enabled via pynvml")
        except ImportError:
            logger.debug("pynvml not available; GPU metrics disabled")
        except Exception:
            logger.debug("Failed to initialize pynvml", exc_info=True)

    def collect_system(self) -> SystemMetrics:
        """Collect system-wide metrics."""
        # CPU (non-blocking delta from previous call)
        cpu_percent = psutil.cpu_percent(interval=None)

        # Memory
        mem = psutil.virtual_memory()
        memory_percent = mem.percent
        memory_used_mb = mem.used / (1024 * 1024)
        memory_total_mb = mem.total / (1024 * 1024)

        # Disk
        disk = psutil.disk_usage('/')
        disk_percent = disk.percent
        disk_used_gb = disk.used / (1024 * 1024 * 1024)
        disk_total_gb = disk.total / (1024 * 1024 * 1024)

        # Load average (Unix-like systems)
        try:
            load_1, load_5, load_15 = os.getloadavg()
        except (AttributeError, OSError):
            load_1 = load_5 = load_15 = 0.0

        # Network throughput (compute delta from last call)
        network_sent_mbps = 0.0
        network_recv_mbps = 0.0
        try:
            net = psutil.net_io_counters()
            now = time.time()
            if self._last_net_io:
                last_time, last_sent, last_recv = self._last_net_io
                delta_time = now - last_time
                if delta_time > 0:
                    delta_sent = net.bytes_sent - last_sent
                    delta_recv = net.bytes_recv - last_recv
                    network_sent_mbps = (delta_sent / delta_time) / (1024 * 1024)
                    network_recv_mbps = (delta_recv / delta_time) / (1024 * 1024)
            self._last_net_io = (now, net.bytes_sent, net.bytes_recv)
        except Exception:
            logger.debug("Failed to collect network metrics", exc_info=True)

        # GPU metrics (optional)
        gpu_name = None
        gpu_utilization = None
        gpu_memory_used_mb = None
        gpu_memory_total_mb = None
        gpu_temperature = None

        if self.gpu_available and self.gpu_handle:
            try:
                import pynvml
                gpu_name = pynvml.nvmlDeviceGetName(self.gpu_handle)
                if isinstance(gpu_name, bytes):
                    gpu_name = gpu_name.decode('utf-8')
                util = pynvml.nvmlDeviceGetUtilizationRates(self.gpu_handle)
                gpu_utilization = float(util.gpu)
                mem_info = pynvml.nvmlDeviceGetMemoryInfo(self.gpu_handle)
                gpu_memory_used_mb = mem_info.used / (1024 * 1024)
                gpu_memory_total_mb = mem_info.total / (1024 * 1024)
                gpu_temperature = float(pynvml.nvmlDeviceGetTemperature(self.gpu_handle, pynvml.NVML_TEMPERATURE_GPU))
            except Exception:
                logger.debug("Failed to collect GPU metrics", exc_info=True)

        return SystemMetrics(
            cpu_percent=cpu_percent,
            memory_percent=memory_percent,
            memory_used_mb=memory_used_mb,
            memory_total_mb=memory_total_mb,
            disk_percent=disk_percent,
            disk_used_gb=disk_used_gb,
            disk_total_gb=disk_total_gb,
            load_avg_1min=load_1,
            load_avg_5min=load_5,
            load_avg_15min=load_15,
            network_sent_mbps=network_sent_mbps,
            network_recv_mbps=network_recv_mbps,
            gpu_name=gpu_name,
            gpu_utilization=gpu_utilization,
            gpu_memory_used_mb=gpu_memory_used_mb,
            gpu_memory_total_mb=gpu_memory_total_mb,
            gpu_temperature=gpu_temperature,
        )

    @staticmethod
    def _get_pane_pid(session_name: str) -> int | None:
        """Get the root process PID for a tmux session's pane."""
        try:
            result = subprocess.run(
                ["tmux", "list-panes", "-t", session_name, "-F", "#{pane_pid}"],
                capture_output=True, text=True, timeout=TMUX_TIMEOUT,
            )
            if result.returncode != 0:
                return None
            # Take the first pane's PID
            pid_str = result.stdout.strip().splitlines()[0]
            return int(pid_str)
        except (subprocess.TimeoutExpired, FileNotFoundError, ValueError, IndexError):
            return None

    def _get_or_cache_proc(self, pid: int) -> psutil.Process | None:
        """Get a cached Process object, or create and warm up a new one."""
        proc = self._proc_cache.get(pid)
        if proc is not None:
            try:
                proc.status()  # verify still alive
                return proc
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                del self._proc_cache[pid]
        try:
            proc = psutil.Process(pid)
            # Warm-up call so the next cpu_percent(interval=0) returns real data
            proc.cpu_percent(interval=0)
            self._proc_cache[pid] = proc
            return proc
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return None

    def collect_agent(self, agent: Agent) -> AgentMetrics | None:
        """Collect resource metrics for a single agent.

        Uses tmux list-panes to get the pane's root PID, then walks
        the process tree via psutil to aggregate CPU/memory usage.
        """
        pane_pid = self._get_pane_pid(agent.session_name)
        if pane_pid is None:
            return AgentMetrics(
                agent_id=agent.id,
                process_count=0,
                cpu_percent=0.0,
                memory_mb=0.0,
            )

        # Collect the pane root process and all its descendants
        all_pids: set[int] = {pane_pid}
        try:
            root = psutil.Process(pane_pid)
            for child in root.children(recursive=True):
                all_pids.add(child.pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

        # Aggregate metrics using cached Process objects
        total_cpu = 0.0
        total_memory = 0.0
        process_count = 0

        for pid in all_pids:
            proc = self._get_or_cache_proc(pid)
            if proc is None:
                continue
            try:
                total_cpu += proc.cpu_percent(interval=0)
                mem_info = proc.memory_info()
                total_memory += mem_info.rss / (1024 * 1024)  # MB
                process_count += 1
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                self._proc_cache.pop(pid, None)

        return AgentMetrics(
            agent_id=agent.id,
            process_count=process_count,
            cpu_percent=total_cpu,
            memory_mb=total_memory,
        )

    def collect_all(self, agent_manager: AgentManager) -> MetricsSnapshot:
        """Collect system metrics and per-agent metrics for all non-stopped agents."""
        system = self.collect_system()
        agents: dict[str, AgentMetrics] = {}
        total_memory = 0.0
        running_count = 0

        from .agent_manager import AgentLocation, AgentStatus

        for agent in agent_manager.list_agents():
            if agent.status == AgentStatus.STOPPED:
                continue

            # Remote agents don't have local processes — return placeholder metrics
            if agent.location == AgentLocation.REMOTE:
                agents[agent.id] = AgentMetrics(
                    agent_id=agent.id,
                    process_count=0,
                    cpu_percent=0.0,
                    memory_mb=0.0,
                )
                running_count += 1
                continue

            agent_metrics = self.collect_agent(agent)
            if agent_metrics:
                agents[agent.id] = agent_metrics
                total_memory += agent_metrics.memory_mb
                running_count += 1

        return MetricsSnapshot(
            timestamp=time.time(),
            system=system,
            agents=agents,
            total_agents_running=running_count,
            total_agent_memory_mb=total_memory,
        )
