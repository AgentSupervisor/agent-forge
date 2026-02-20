"""Tests for remote execution config models and validate command."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from agent_forge.config import ForgeConfig, ProjectConfig, RemoteConfig
from agent_forge.registry import ProjectRegistry


# ---------------------------------------------------------------------------
# RemoteConfig model
# ---------------------------------------------------------------------------


class TestRemoteConfig:
    def test_defaults(self):
        rc = RemoteConfig()
        assert rc.docker_context == "vm"
        assert rc.vm_ip == ""
        assert rc.image == ""
        assert rc.ttyd_port_range_start == 30000
        assert rc.ttyd_port_range_end == 32767
        assert rc.cleanup_after_hours == 24
        assert rc.cpu_limit == "1"
        assert rc.memory_limit == "2G"
        assert rc.config_repo == ""
        assert rc.ttyd_user == "agent"
        assert rc.ttyd_pass_env == "AGENT_TTYD_PASS"

    def test_custom_values(self):
        rc = RemoteConfig(
            docker_context="prod",
            vm_ip="10.0.0.1",
            image="agent:latest",
            ttyd_port_range_start=40000,
            ttyd_port_range_end=41000,
            cleanup_after_hours=48,
            cpu_limit="2",
            memory_limit="4G",
            config_repo="git@github.com:org/config.git",
            ttyd_user="admin",
            ttyd_pass_env="MY_PASS",
        )
        assert rc.docker_context == "prod"
        assert rc.vm_ip == "10.0.0.1"
        assert rc.image == "agent:latest"
        assert rc.ttyd_port_range_start == 40000
        assert rc.ttyd_port_range_end == 41000
        assert rc.cleanup_after_hours == 48
        assert rc.cpu_limit == "2"
        assert rc.memory_limit == "4G"
        assert rc.config_repo == "git@github.com:org/config.git"
        assert rc.ttyd_user == "admin"
        assert rc.ttyd_pass_env == "MY_PASS"

    def test_serialization_roundtrip(self):
        rc = RemoteConfig(vm_ip="1.2.3.4", image="img:v1")
        data = rc.model_dump()
        rc2 = RemoteConfig(**data)
        assert rc == rc2


# ---------------------------------------------------------------------------
# ForgeConfig with remote
# ---------------------------------------------------------------------------


class TestForgeConfigRemote:
    def test_no_remote_by_default(self):
        cfg = ForgeConfig()
        assert cfg.remote is None

    def test_with_remote(self):
        cfg = ForgeConfig(remote={"docker_context": "swarm", "vm_ip": "10.0.0.5"})
        assert cfg.remote is not None
        assert cfg.remote.docker_context == "swarm"
        assert cfg.remote.vm_ip == "10.0.0.5"

    def test_remote_none_explicit(self):
        cfg = ForgeConfig(remote=None)
        assert cfg.remote is None


# ---------------------------------------------------------------------------
# ProjectConfig execution field
# ---------------------------------------------------------------------------


class TestProjectConfigExecution:
    def test_default_execution(self, tmp_path):
        (tmp_path / ".git").mkdir()
        pc = ProjectConfig(path=str(tmp_path))
        assert pc.execution == "local"
        assert pc.execution_reason == ""

    def test_remote_execution(self, tmp_path):
        (tmp_path / ".git").mkdir()
        pc = ProjectConfig(path=str(tmp_path), execution="remote", execution_reason="GPU needed")
        assert pc.execution == "remote"
        assert pc.execution_reason == "GPU needed"

    def test_invalid_execution(self, tmp_path):
        with pytest.raises(ValueError, match="execution must be one of"):
            ProjectConfig(path=str(tmp_path), execution="cloud")


# ---------------------------------------------------------------------------
# Registry warns on remote without config
# ---------------------------------------------------------------------------


class TestRegistryRemoteValidation:
    def test_warns_remote_without_config(self, tmp_path, caplog):
        git_dir = tmp_path / "repo" / ".git"
        git_dir.mkdir(parents=True)

        config_data = {
            "projects": {
                "my-proj": {
                    "path": str(tmp_path / "repo"),
                    "execution": "remote",
                }
            }
        }
        config_file = tmp_path / "config.yaml"
        with open(config_file, "w") as f:
            yaml.dump(config_data, f)

        import logging
        with caplog.at_level(logging.WARNING):
            registry = ProjectRegistry(config_path=str(config_file))

        assert any(
            "execution is 'remote' but no remote config defined" in msg
            for msg in caplog.messages
        )

    def test_no_warning_remote_with_config(self, tmp_path, caplog):
        git_dir = tmp_path / "repo" / ".git"
        git_dir.mkdir(parents=True)

        config_data = {
            "remote": {"docker_context": "vm", "vm_ip": "10.0.0.1"},
            "projects": {
                "my-proj": {
                    "path": str(tmp_path / "repo"),
                    "execution": "remote",
                }
            },
        }
        config_file = tmp_path / "config.yaml"
        with open(config_file, "w") as f:
            yaml.dump(config_data, f)

        import logging
        with caplog.at_level(logging.WARNING):
            registry = ProjectRegistry(config_path=str(config_file))

        assert not any(
            "execution is 'remote' but no remote config defined" in msg
            for msg in caplog.messages
        )

    def test_no_warning_local_without_remote_config(self, tmp_path, caplog):
        git_dir = tmp_path / "repo" / ".git"
        git_dir.mkdir(parents=True)

        config_data = {
            "projects": {
                "my-proj": {
                    "path": str(tmp_path / "repo"),
                    "execution": "local",
                }
            }
        }
        config_file = tmp_path / "config.yaml"
        with open(config_file, "w") as f:
            yaml.dump(config_data, f)

        import logging
        with caplog.at_level(logging.WARNING):
            registry = ProjectRegistry(config_path=str(config_file))

        assert not any(
            "remote" in msg.lower()
            for msg in caplog.messages
        )


# ---------------------------------------------------------------------------
# forge remote validate command
# ---------------------------------------------------------------------------


class TestRemoteValidate:
    @pytest.fixture
    def remote_config_file(self, tmp_path):
        git_dir = tmp_path / "repo" / ".git"
        git_dir.mkdir(parents=True)

        config_data = {
            "remote": {
                "docker_context": "vm",
                "vm_ip": "10.0.0.1",
                "image": "agent:latest",
                "config_repo": "git@github.com:org/config.git",
                "ttyd_pass_env": "AGENT_TTYD_PASS",
            },
            "projects": {
                "test": {
                    "path": str(tmp_path / "repo"),
                    "execution": "remote",
                }
            },
        }
        config_file = tmp_path / "config.yaml"
        with open(config_file, "w") as f:
            yaml.dump(config_data, f)
        return str(config_file)

    @pytest.fixture
    def no_remote_config_file(self, tmp_path):
        git_dir = tmp_path / "repo" / ".git"
        git_dir.mkdir(parents=True)

        config_data = {
            "projects": {
                "test": {
                    "path": str(tmp_path / "repo"),
                }
            },
        }
        config_file = tmp_path / "config.yaml"
        with open(config_file, "w") as f:
            yaml.dump(config_data, f)
        return str(config_file)

    def test_no_remote_config_exits(self, no_remote_config_file, capsys):
        from agent_forge.cli import cmd_remote_validate
        import argparse

        args = argparse.Namespace(config=no_remote_config_file)
        with pytest.raises(SystemExit) as exc_info:
            cmd_remote_validate(args)
        assert exc_info.value.code == 1

        captured = capsys.readouterr()
        assert "No remote config found" in captured.out

    def test_all_checks_pass(self, remote_config_file, capsys, tmp_path):
        from agent_forge.cli import cmd_remote_validate
        import argparse

        def mock_run(cmd, **kwargs):
            mock = MagicMock()
            mock.returncode = 0
            mock.stdout = "ok"
            mock.stderr = ""
            return mock

        ssh_key = tmp_path / "fake_ssh"
        creds = tmp_path / "fake_creds"

        env = {
            "CLAUDE_CODE_OAUTH_TOKEN": "tok",
            "GITHUB_TOKEN": "gh-tok",
            "AGENT_TTYD_PASS": "secret",
        }

        args = argparse.Namespace(config=remote_config_file)
        with (
            patch("agent_forge.cli.subprocess.run", side_effect=mock_run),
            patch.dict("os.environ", env, clear=False),
            patch("agent_forge.cli.Path.home", return_value=tmp_path),
        ):
            # Create fake SSH key and credentials
            (tmp_path / ".ssh").mkdir()
            (tmp_path / ".ssh" / "id_rsa").touch()
            (tmp_path / ".claude").mkdir()
            (tmp_path / ".claude" / ".credentials.json").touch()

            cmd_remote_validate(args)

        captured = capsys.readouterr()
        assert "All checks passed" in captured.out
        assert "FAIL" not in captured.out

    def test_mixed_failures(self, remote_config_file, capsys, tmp_path):
        from agent_forge.cli import cmd_remote_validate
        import argparse

        call_count = 0

        def mock_run(cmd, **kwargs):
            nonlocal call_count
            call_count += 1
            mock = MagicMock()
            # Docker context passes, image fails, git ls-remote fails
            if "info" in cmd:
                mock.returncode = 0
            else:
                mock.returncode = 1
            mock.stdout = ""
            mock.stderr = "error"
            return mock

        env = {
            "GITHUB_TOKEN": "gh-tok",
            # Missing: CLAUDE_CODE_OAUTH_TOKEN, AGENT_TTYD_PASS
        }

        args = argparse.Namespace(config=remote_config_file)
        with (
            patch("agent_forge.cli.subprocess.run", side_effect=mock_run),
            patch.dict("os.environ", env, clear=False),
            patch("agent_forge.cli.Path.home", return_value=tmp_path),
        ):
            # No SSH key, no credentials
            with pytest.raises(SystemExit) as exc_info:
                cmd_remote_validate(args)
            assert exc_info.value.code == 1

        captured = capsys.readouterr()
        assert "PASS" in captured.out
        assert "FAIL" in captured.out
        assert "issue(s) found" in captured.out

    def test_docker_not_found(self, remote_config_file, capsys, tmp_path):
        from agent_forge.cli import cmd_remote_validate
        import argparse

        def mock_run(cmd, **kwargs):
            if cmd[0] == "docker":
                raise FileNotFoundError("docker not found")
            mock = MagicMock()
            mock.returncode = 0
            return mock

        env = {
            "CLAUDE_CODE_OAUTH_TOKEN": "tok",
            "GITHUB_TOKEN": "gh-tok",
            "AGENT_TTYD_PASS": "secret",
        }

        args = argparse.Namespace(config=remote_config_file)
        with (
            patch("agent_forge.cli.subprocess.run", side_effect=mock_run),
            patch.dict("os.environ", env, clear=False),
            patch("agent_forge.cli.Path.home", return_value=tmp_path),
        ):
            (tmp_path / ".ssh").mkdir()
            (tmp_path / ".ssh" / "id_rsa").touch()

            with pytest.raises(SystemExit) as exc_info:
                cmd_remote_validate(args)
            assert exc_info.value.code == 1

        captured = capsys.readouterr()
        assert "docker not found" in captured.out

    def test_config_file_not_found(self, tmp_path, capsys):
        from agent_forge.cli import cmd_remote_validate
        import argparse

        args = argparse.Namespace(config=str(tmp_path / "nonexistent.yaml"))
        with pytest.raises(SystemExit) as exc_info:
            cmd_remote_validate(args)
        assert exc_info.value.code == 1

        captured = capsys.readouterr()
        assert "Config not found" in captured.out
