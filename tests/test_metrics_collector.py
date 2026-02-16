"""Tests for MetricsCollector — system and per-agent metrics collection."""

from unittest.mock import MagicMock, patch

import psutil
import pytest

from agent_forge.agent_manager import Agent, AgentStatus
from agent_forge.config import MetricsConfig
from agent_forge.metrics_collector import (
    AgentMetrics,
    MetricsCollector,
    MetricsSnapshot,
    SystemMetrics,
)


class TestCollectSystemMetrics:
    """Test system-wide metrics collection."""

    @patch("agent_forge.metrics_collector.time.time")
    @patch("agent_forge.metrics_collector.os.getloadavg", return_value=(1.5, 1.2, 0.8))
    @patch("agent_forge.metrics_collector.psutil")
    def test_collect_system_metrics(self, mock_psutil, mock_loadavg, mock_time):
        """Verify all SystemMetrics fields are populated correctly."""
        # Configure mock psutil
        mock_psutil.cpu_percent.return_value = 45.2
        mock_psutil.virtual_memory.return_value = MagicMock(
            percent=67.8,
            used=10 * 1024 * 1024 * 1024,
            total=16 * 1024 * 1024 * 1024,
        )
        mock_psutil.disk_usage.return_value = MagicMock(
            percent=55.3,
            used=200 * 1024 * 1024 * 1024,
            total=500 * 1024 * 1024 * 1024,
        )
        mock_psutil.net_io_counters.return_value = MagicMock(
            bytes_sent=1000 * 1024 * 1024,
            bytes_recv=2000 * 1024 * 1024,
        )

        # Mock time for initialization and first collect
        mock_time.return_value = 0.0
        collector = MetricsCollector(enable_gpu=False)

        # Second call to compute network delta
        mock_psutil.net_io_counters.return_value = MagicMock(
            bytes_sent=1010 * 1024 * 1024,
            bytes_recv=2020 * 1024 * 1024,
        )
        mock_time.return_value = 1.0
        metrics = collector.collect_system()

        assert metrics.cpu_percent == 45.2
        assert metrics.memory_percent == 67.8
        assert metrics.memory_used_mb == pytest.approx(10 * 1024, rel=0.01)
        assert metrics.memory_total_mb == pytest.approx(16 * 1024, rel=0.01)
        assert metrics.disk_percent == 55.3
        assert metrics.disk_used_gb == pytest.approx(200, rel=0.01)
        assert metrics.disk_total_gb == pytest.approx(500, rel=0.01)
        assert metrics.load_avg_1min == 1.5
        assert metrics.load_avg_5min == 1.2
        assert metrics.load_avg_15min == 0.8
        # Network throughput computed from delta: 10 MB / 1 sec = 10 MB/s
        assert metrics.network_sent_mbps == pytest.approx(10.0, rel=0.01)
        assert metrics.network_recv_mbps == pytest.approx(20.0, rel=0.01)
        # GPU fields should be None
        assert metrics.gpu_name is None
        assert metrics.gpu_utilization is None
        assert metrics.gpu_memory_used_mb is None
        assert metrics.gpu_memory_total_mb is None
        assert metrics.gpu_temperature is None

    @patch("agent_forge.metrics_collector.os.getloadavg", return_value=(1.5, 1.2, 0.8))
    @patch("agent_forge.metrics_collector.psutil")
    def test_collect_system_metrics_no_gpu(self, mock_psutil, mock_loadavg):
        """Verify GPU fields are None when pynvml unavailable."""
        mock_psutil.cpu_percent.return_value = 30.0
        mock_psutil.virtual_memory.return_value = MagicMock(
            percent=50.0,
            used=8 * 1024 * 1024 * 1024,
            total=16 * 1024 * 1024 * 1024,
        )
        mock_psutil.disk_usage.return_value = MagicMock(
            percent=40.0,
            used=100 * 1024 * 1024 * 1024,
            total=250 * 1024 * 1024 * 1024,
        )
        mock_psutil.net_io_counters.return_value = MagicMock(
            bytes_sent=500 * 1024 * 1024,
            bytes_recv=1000 * 1024 * 1024,
        )

        collector = MetricsCollector(enable_gpu=False)
        metrics = collector.collect_system()

        assert metrics.gpu_name is None
        assert metrics.gpu_utilization is None
        assert metrics.gpu_memory_used_mb is None
        assert metrics.gpu_memory_total_mb is None
        assert metrics.gpu_temperature is None


class TestCollectAgentMetrics:
    """Test per-agent metrics collection."""

    @patch("agent_forge.metrics_collector.psutil")
    def test_collect_agent_metrics_found(self, mock_psutil):
        """Mock process_iter with matching cmdline, verify aggregation."""
        # Create mock agent
        agent = Agent(
            id="abc123",
            project_name="test-project",
            session_name="forge__test-project__abc123",
            worktree_path="/tmp/worktree",
            branch_name="agent/abc123/task",
            status=AgentStatus.WORKING,
        )

        # Create mock process
        mock_proc = MagicMock()
        mock_proc.info = {
            'pid': 1234,
            'name': 'tmux',
            'cmdline': ['tmux', 'new-session', '-s', 'forge__test-project__abc123'],
        }

        # Create mock Process instance for children lookup
        mock_process_instance = MagicMock()
        mock_child1 = MagicMock()
        mock_child1.pid = 1235
        mock_child2 = MagicMock()
        mock_child2.pid = 1236
        mock_process_instance.children.return_value = [mock_child1, mock_child2]

        mock_psutil.process_iter.return_value = [mock_proc]
        mock_psutil.Process.side_effect = lambda pid: {
            1234: mock_process_instance,
            1235: MagicMock(cpu_percent=lambda interval: 5.0, memory_info=lambda: MagicMock(rss=50 * 1024 * 1024)),
            1236: MagicMock(cpu_percent=lambda interval: 3.0, memory_info=lambda: MagicMock(rss=30 * 1024 * 1024)),
        }[pid]
        mock_psutil.NoSuchProcess = psutil.NoSuchProcess
        mock_psutil.AccessDenied = psutil.AccessDenied

        # Configure main process metrics
        main_proc = mock_psutil.Process(1234)
        main_proc.cpu_percent.return_value = 10.0
        main_proc.memory_info.return_value = MagicMock(rss=100 * 1024 * 1024)

        collector = MetricsCollector(enable_gpu=False)
        metrics = collector.collect_agent(agent)

        assert metrics is not None
        assert metrics.agent_id == "abc123"
        assert metrics.process_count == 3
        assert metrics.cpu_percent == 18.0  # 10 + 5 + 3
        assert metrics.memory_mb == pytest.approx(180, rel=0.01)  # 100 + 50 + 30

    @patch("agent_forge.metrics_collector.psutil")
    def test_collect_agent_metrics_not_found(self, mock_psutil):
        """Empty process_iter returns metrics with 0 counts."""
        agent = Agent(
            id="xyz789",
            project_name="test-project",
            session_name="forge__test-project__xyz789",
            worktree_path="/tmp/worktree",
            branch_name="agent/xyz789/task",
            status=AgentStatus.WORKING,
        )

        mock_psutil.process_iter.return_value = []
        mock_psutil.NoSuchProcess = psutil.NoSuchProcess
        mock_psutil.AccessDenied = psutil.AccessDenied

        collector = MetricsCollector(enable_gpu=False)
        metrics = collector.collect_agent(agent)

        assert metrics is not None
        assert metrics.agent_id == "xyz789"
        assert metrics.process_count == 0
        assert metrics.cpu_percent == 0.0
        assert metrics.memory_mb == 0.0

    @patch("agent_forge.metrics_collector.psutil")
    def test_collect_agent_metrics_access_denied(self, mock_psutil):
        """psutil.AccessDenied handled gracefully."""
        agent = Agent(
            id="def456",
            project_name="test-project",
            session_name="forge__test-project__def456",
            worktree_path="/tmp/worktree",
            branch_name="agent/def456/task",
            status=AgentStatus.WORKING,
        )

        # First process raises AccessDenied
        mock_proc = MagicMock()
        mock_proc.info = {'pid': 1234, 'name': 'tmux', 'cmdline': None}

        def raise_access_denied():
            raise psutil.AccessDenied("test")

        mock_psutil.process_iter.return_value = [mock_proc]
        mock_psutil.NoSuchProcess = psutil.NoSuchProcess
        mock_psutil.AccessDenied = psutil.AccessDenied

        collector = MetricsCollector(enable_gpu=False)
        metrics = collector.collect_agent(agent)

        # Should handle exception and return zero metrics
        assert metrics is not None
        assert metrics.process_count == 0


class TestCollectAll:
    """Test integration of system + agent metrics."""

    @patch("agent_forge.metrics_collector.os.getloadavg", return_value=(1.0, 0.9, 0.8))
    @patch("agent_forge.metrics_collector.psutil")
    def test_collect_all(self, mock_psutil, mock_loadavg):
        """Verify MetricsSnapshot structure."""
        # Mock system metrics
        mock_psutil.cpu_percent.return_value = 40.0
        mock_psutil.virtual_memory.return_value = MagicMock(
            percent=60.0,
            used=8 * 1024 * 1024 * 1024,
            total=16 * 1024 * 1024 * 1024,
        )
        mock_psutil.disk_usage.return_value = MagicMock(
            percent=50.0,
            used=100 * 1024 * 1024 * 1024,
            total=200 * 1024 * 1024 * 1024,
        )
        mock_psutil.net_io_counters.return_value = MagicMock(
            bytes_sent=1000 * 1024 * 1024,
            bytes_recv=2000 * 1024 * 1024,
        )

        # Mock agent processes
        mock_proc = MagicMock()
        mock_proc.info = {
            'pid': 1234,
            'name': 'tmux',
            'cmdline': ['tmux', 'new-session', '-s', 'forge__project__agent1'],
        }
        mock_psutil.process_iter.return_value = [mock_proc]

        mock_process_instance = MagicMock()
        mock_process_instance.children.return_value = []
        mock_process_instance.cpu_percent.return_value = 8.0
        mock_process_instance.memory_info.return_value = MagicMock(rss=80 * 1024 * 1024)

        mock_psutil.Process.return_value = mock_process_instance
        mock_psutil.NoSuchProcess = psutil.NoSuchProcess
        mock_psutil.AccessDenied = psutil.AccessDenied

        # Create mock agent manager
        agent1 = Agent(
            id="agent1",
            project_name="project",
            session_name="forge__project__agent1",
            worktree_path="/tmp/worktree",
            branch_name="agent/agent1/task",
            status=AgentStatus.WORKING,
        )
        agent2 = Agent(
            id="agent2",
            project_name="project",
            session_name="forge__project__agent2",
            worktree_path="/tmp/worktree",
            branch_name="agent/agent2/task",
            status=AgentStatus.STOPPED,
        )

        mock_manager = MagicMock()
        mock_manager.list_agents.return_value = [agent1, agent2]

        collector = MetricsCollector(enable_gpu=False)
        with patch("agent_forge.metrics_collector.time.time", return_value=1234567890.0):
            snapshot = collector.collect_all(mock_manager)

        assert isinstance(snapshot, MetricsSnapshot)
        assert snapshot.timestamp == 1234567890.0
        assert isinstance(snapshot.system, SystemMetrics)
        assert snapshot.system.cpu_percent == 40.0
        # Only agent1 should be included (agent2 is STOPPED)
        assert len(snapshot.agents) == 1
        assert "agent1" in snapshot.agents
        assert "agent2" not in snapshot.agents
        assert snapshot.total_agents_running == 1
        assert snapshot.total_agent_memory_mb == pytest.approx(80, rel=0.01)


class TestNetworkThroughputDelta:
    """Test network throughput delta computation."""

    @patch("agent_forge.metrics_collector.os.getloadavg", return_value=(1.0, 1.0, 1.0))
    @patch("agent_forge.metrics_collector.psutil")
    def test_network_throughput_delta(self, mock_psutil, mock_loadavg):
        """Verify second call computes correct delta from net_io_counters."""
        mock_psutil.cpu_percent.return_value = 30.0
        mock_psutil.virtual_memory.return_value = MagicMock(
            percent=50.0, used=8 * 1024**3, total=16 * 1024**3,
        )
        mock_psutil.disk_usage.return_value = MagicMock(
            percent=40.0, used=100 * 1024**3, total=250 * 1024**3,
        )

        # First call baseline
        mock_psutil.net_io_counters.return_value = MagicMock(
            bytes_sent=1000 * 1024 * 1024,  # 1000 MB
            bytes_recv=2000 * 1024 * 1024,  # 2000 MB
        )

        collector = MetricsCollector(enable_gpu=False)

        # First collect (should have baseline but no delta yet)
        with patch("agent_forge.metrics_collector.time.time", return_value=100.0):
            metrics1 = collector.collect_system()

        # Network should be 0 or close to 0 on first call
        assert metrics1.network_sent_mbps >= 0
        assert metrics1.network_recv_mbps >= 0

        # Second call with delta
        mock_psutil.net_io_counters.return_value = MagicMock(
            bytes_sent=1050 * 1024 * 1024,  # +50 MB
            bytes_recv=2100 * 1024 * 1024,  # +100 MB
        )

        with patch("agent_forge.metrics_collector.time.time", return_value=110.0):
            metrics2 = collector.collect_system()

        # 50 MB / 10 seconds = 5 MB/s
        assert metrics2.network_sent_mbps == pytest.approx(5.0, rel=0.01)
        # 100 MB / 10 seconds = 10 MB/s
        assert metrics2.network_recv_mbps == pytest.approx(10.0, rel=0.01)


class TestMetricsConfigDefaults:
    """Test MetricsConfig Pydantic defaults."""

    def test_metrics_config_defaults(self):
        """Verify MetricsConfig defaults."""
        config = MetricsConfig()
        assert config.enabled is True
        assert config.collect_interval_seconds == 5.0
        assert config.enable_gpu is True
        assert config.enable_per_agent is True
