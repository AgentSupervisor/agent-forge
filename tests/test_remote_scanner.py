"""Tests for toolchain scanner and image builder."""

from __future__ import annotations

import argparse
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from agent_forge.config import ForgeConfig, RemoteConfig
from agent_forge.remote_image_builder import generate_dockerfile
from agent_forge.remote_scanner import (
    scan_all_projects,
    scan_local_utilities,
    scan_project,
    save_manifest,
    load_manifest,
)


# ---------------------------------------------------------------------------
# scan_project
# ---------------------------------------------------------------------------


class TestScanProject:
    def test_detects_python(self, tmp_path):
        (tmp_path / "requirements.txt").touch()
        result = scan_project(str(tmp_path))
        assert "requirements.txt" in result["markers"]
        assert any(t["name"] == "python" for t in result["toolchains"])

    def test_detects_go(self, tmp_path):
        (tmp_path / "go.mod").touch()
        result = scan_project(str(tmp_path))
        assert "go.mod" in result["markers"]
        assert any(t["name"] == "go" for t in result["toolchains"])

    def test_detects_multiple_toolchains(self, tmp_path):
        (tmp_path / "pyproject.toml").touch()
        (tmp_path / "Cargo.toml").touch()
        (tmp_path / "Makefile").touch()
        result = scan_project(str(tmp_path))
        names = {t["name"] for t in result["toolchains"]}
        assert "python" in names
        assert "rust" in names
        assert "c-cpp" in names

    def test_no_markers(self, tmp_path):
        result = scan_project(str(tmp_path))
        assert result["markers"] == []
        assert result["toolchains"] == []

    def test_nonexistent_path(self):
        result = scan_project("/nonexistent/path/abc123")
        assert result["markers"] == []
        assert result["toolchains"] == []

    def test_no_duplicate_toolchains(self, tmp_path):
        # Both markers for the same toolchain
        (tmp_path / "requirements.txt").touch()
        (tmp_path / "pyproject.toml").touch()
        result = scan_project(str(tmp_path))
        python_count = sum(1 for t in result["toolchains"] if t["name"] == "python")
        assert python_count == 1

    def test_detects_java(self, tmp_path):
        (tmp_path / "pom.xml").touch()
        result = scan_project(str(tmp_path))
        assert any(t["name"] == "java" for t in result["toolchains"])

    def test_detects_ruby(self, tmp_path):
        (tmp_path / "Gemfile").touch()
        result = scan_project(str(tmp_path))
        assert any(t["name"] == "ruby" for t in result["toolchains"])

    def test_detects_docker_compose(self, tmp_path):
        (tmp_path / "docker-compose.yml").touch()
        result = scan_project(str(tmp_path))
        assert any(t["name"] == "docker-cli" for t in result["toolchains"])

    def test_rust_has_install_script(self, tmp_path):
        (tmp_path / "Cargo.toml").touch()
        result = scan_project(str(tmp_path))
        rust = next(t for t in result["toolchains"] if t["name"] == "rust")
        assert "install_script" in rust
        assert "rustup" in rust["install_script"]


# ---------------------------------------------------------------------------
# scan_local_utilities
# ---------------------------------------------------------------------------


class TestScanLocalUtilities:
    def test_detects_installed_utils(self):
        def fake_check(name):
            return name in ("jq", "ripgrep")

        result = scan_local_utilities(check_bin_fn=fake_check)
        names = {u["name"] for u in result}
        assert "jq" in names
        assert "ripgrep" in names
        assert "wget" not in names

    def test_no_utils_found(self):
        result = scan_local_utilities(check_bin_fn=lambda _: False)
        assert result == []


# ---------------------------------------------------------------------------
# scan_all_projects
# ---------------------------------------------------------------------------


class TestScanAllProjects:
    def test_skips_local_projects(self, tmp_path):
        local_dir = tmp_path / "local"
        local_dir.mkdir()
        (local_dir / ".git").mkdir()

        remote_dir = tmp_path / "remote"
        remote_dir.mkdir()
        (remote_dir / ".git").mkdir()
        (remote_dir / "requirements.txt").touch()

        config = ForgeConfig(
            projects={
                "local-proj": {
                    "path": str(local_dir),
                    "execution": "local",
                },
                "remote-proj": {
                    "path": str(remote_dir),
                    "execution": "remote",
                },
            }
        )

        manifest = scan_all_projects(config, include_local_utils=False)

        scanned = manifest["scanned_projects"]
        local_entry = next(p for p in scanned if p["name"] == "local-proj")
        remote_entry = next(p for p in scanned if p["name"] == "remote-proj")

        assert local_entry["skipped"] is True
        assert "markers" not in local_entry
        assert "requirements.txt" in remote_entry["markers"]
        assert len(manifest["toolchains"]) == 1
        assert manifest["toolchains"][0]["name"] == "python"

    def test_includes_utilities(self, tmp_path):
        proj_dir = tmp_path / "proj"
        proj_dir.mkdir()
        (proj_dir / ".git").mkdir()

        config = ForgeConfig(
            projects={"proj": {"path": str(proj_dir), "execution": "remote"}}
        )

        manifest = scan_all_projects(
            config,
            include_local_utils=True,
            check_bin_fn=lambda name: name == "jq",
        )
        assert any(u["name"] == "jq" for u in manifest["utilities"])

    def test_no_utilities_when_disabled(self, tmp_path):
        proj_dir = tmp_path / "proj"
        proj_dir.mkdir()
        (proj_dir / ".git").mkdir()

        config = ForgeConfig(
            projects={"proj": {"path": str(proj_dir), "execution": "remote"}}
        )

        manifest = scan_all_projects(config, include_local_utils=False)
        assert manifest["utilities"] == []

    def test_manifest_has_required_keys(self, tmp_path):
        proj_dir = tmp_path / "proj"
        proj_dir.mkdir()
        (proj_dir / ".git").mkdir()

        config = ForgeConfig(
            projects={"proj": {"path": str(proj_dir), "execution": "remote"}}
        )

        manifest = scan_all_projects(config, include_local_utils=False)
        assert "generated_at" in manifest
        assert "scanned_projects" in manifest
        assert "toolchains" in manifest
        assert "utilities" in manifest
        assert "manual" in manifest
        assert manifest["manual"] == {"apt": [], "npm_global": [], "pip": []}


# ---------------------------------------------------------------------------
# save_manifest / load_manifest
# ---------------------------------------------------------------------------


class TestManifestIO:
    def test_save_and_load(self, tmp_path):
        manifest = {
            "generated_at": "2026-02-20T00:00:00Z",
            "toolchains": [{"name": "python", "packages_apt": ["python3"]}],
            "utilities": [],
            "manual": {"apt": [], "npm_global": [], "pip": []},
        }
        path = str(tmp_path / "manifest.yaml")
        save_manifest(manifest, path)
        loaded = load_manifest(path)
        assert loaded["toolchains"][0]["name"] == "python"

    def test_load_nonexistent(self, tmp_path):
        result = load_manifest(str(tmp_path / "nope.yaml"))
        assert result is None

    def test_save_creates_parent_dirs(self, tmp_path):
        path = str(tmp_path / "sub" / "dir" / "manifest.yaml")
        save_manifest({"test": True}, path)
        assert Path(path).exists()


# ---------------------------------------------------------------------------
# generate_dockerfile
# ---------------------------------------------------------------------------


class TestGenerateDockerfile:
    def test_core_layer_always_present(self):
        manifest = {"toolchains": [], "utilities": [], "manual": {"apt": [], "npm_global": [], "pip": []}}
        df = generate_dockerfile(manifest)
        assert "FROM node:20-bookworm-slim" in df
        assert "git tmux openssh-client curl" in df
        assert "@anthropic-ai/claude-code" in df
        assert "ttyd" in df
        assert 'ENTRYPOINT ["/entrypoint.sh"]' in df

    def test_includes_toolchain_packages(self):
        manifest = {
            "toolchains": [
                {"name": "python", "packages_apt": ["python3", "python3-pip"]},
            ],
            "utilities": [],
            "manual": {"apt": [], "npm_global": [], "pip": []},
        }
        df = generate_dockerfile(manifest)
        assert "python3 python3-pip" in df
        assert "Detected toolchains" in df

    def test_includes_install_script(self):
        manifest = {
            "toolchains": [
                {"name": "rust", "packages_apt": [], "install_script": "curl rustup"},
            ],
            "utilities": [],
            "manual": {"apt": [], "npm_global": [], "pip": []},
        }
        df = generate_dockerfile(manifest)
        assert "RUN curl rustup" in df

    def test_includes_utilities(self):
        manifest = {
            "toolchains": [],
            "utilities": [
                {"name": "jq", "packages_apt": ["jq"]},
                {"name": "ripgrep", "packages_apt": ["ripgrep"]},
            ],
            "manual": {"apt": [], "npm_global": [], "pip": []},
        }
        df = generate_dockerfile(manifest)
        assert "jq ripgrep" in df
        assert "Detected utilities" in df

    def test_includes_manual_apt(self):
        manifest = {
            "toolchains": [],
            "utilities": [],
            "manual": {"apt": ["strace"], "npm_global": [], "pip": []},
        }
        df = generate_dockerfile(manifest)
        assert "strace" in df

    def test_includes_manual_npm(self):
        manifest = {
            "toolchains": [],
            "utilities": [],
            "manual": {"apt": [], "npm_global": ["typescript"], "pip": []},
        }
        df = generate_dockerfile(manifest)
        assert "npm install -g typescript" in df

    def test_includes_manual_pip(self):
        manifest = {
            "toolchains": [],
            "utilities": [],
            "manual": {"apt": [], "npm_global": [], "pip": ["flask"]},
        }
        df = generate_dockerfile(manifest)
        assert "pip3 install" in df
        assert "flask" in df

    def test_deduplicates_apt_packages(self):
        manifest = {
            "toolchains": [
                {"name": "python", "packages_apt": ["python3"]},
            ],
            "utilities": [],
            "manual": {"apt": ["python3"], "npm_global": [], "pip": []},
        }
        df = generate_dockerfile(manifest)
        # python3 should appear only once in the toolchains section
        toolchain_section = df.split("Detected toolchains")[1].split("ENTRYPOINT")[0]
        assert toolchain_section.count("python3") == 1

    def test_empty_manifest(self):
        manifest = {"toolchains": [], "utilities": [], "manual": {"apt": [], "npm_global": [], "pip": []}}
        df = generate_dockerfile(manifest)
        assert "FROM node:20-bookworm-slim" in df
        assert "Detected toolchains" not in df
        assert "Detected utilities" not in df


# ---------------------------------------------------------------------------
# CLI: forge remote scan
# ---------------------------------------------------------------------------


class TestCmdRemoteScan:
    def test_scan_dry_run(self, tmp_path):
        from agent_forge.cli import cmd_remote_scan

        proj_dir = tmp_path / "repo"
        proj_dir.mkdir()
        (proj_dir / ".git").mkdir()
        (proj_dir / "requirements.txt").touch()

        config_data = {
            "projects": {
                "my-proj": {
                    "path": str(proj_dir),
                    "execution": "remote",
                }
            }
        }
        config_file = tmp_path / "config.yaml"
        with open(config_file, "w") as f:
            yaml.dump(config_data, f)

        args = argparse.Namespace(
            config=str(config_file),
            dry_run=True,
            no_local=True,
        )
        cmd_remote_scan(args)

        # No manifest file should be written in dry-run mode
        manifest_path = tmp_path / ".forge" / "toolchain-manifest.yaml"
        assert not manifest_path.exists()

    def test_scan_writes_manifest(self, tmp_path):
        from agent_forge.cli import cmd_remote_scan, MANIFEST_PATH

        proj_dir = tmp_path / "repo"
        proj_dir.mkdir()
        (proj_dir / ".git").mkdir()
        (proj_dir / "go.mod").touch()

        config_data = {
            "projects": {
                "my-proj": {
                    "path": str(proj_dir),
                    "execution": "remote",
                }
            }
        }
        config_file = tmp_path / "config.yaml"
        with open(config_file, "w") as f:
            yaml.dump(config_data, f)

        manifest_out = str(tmp_path / "manifest.yaml")

        args = argparse.Namespace(
            config=str(config_file),
            dry_run=False,
            no_local=True,
        )
        with patch("agent_forge.cli.MANIFEST_PATH", manifest_out):
            cmd_remote_scan(args)

        assert Path(manifest_out).exists()
        loaded = yaml.safe_load(Path(manifest_out).read_text())
        assert any(t["name"] == "go" for t in loaded["toolchains"])


# ---------------------------------------------------------------------------
# CLI: forge remote build-image
# ---------------------------------------------------------------------------


class TestCmdRemoteBuildImage:
    def test_no_remote_config_exits(self, tmp_path, capsys):
        from agent_forge.cli import cmd_remote_build_image

        proj_dir = tmp_path / "repo"
        proj_dir.mkdir()
        (proj_dir / ".git").mkdir()

        config_data = {
            "projects": {"proj": {"path": str(proj_dir)}}
        }
        config_file = tmp_path / "config.yaml"
        with open(config_file, "w") as f:
            yaml.dump(config_data, f)

        args = argparse.Namespace(
            config=str(config_file),
            scan=False,
            no_push=False,
            no_cache=False,
        )
        with pytest.raises(SystemExit) as exc_info:
            cmd_remote_build_image(args)
        assert exc_info.value.code == 1

    def test_build_image_no_push(self, tmp_path, capsys):
        from agent_forge.cli import cmd_remote_build_image

        proj_dir = tmp_path / "repo"
        proj_dir.mkdir()
        (proj_dir / ".git").mkdir()

        config_data = {
            "remote": {"docker_context": "vm", "image": "test:latest"},
            "projects": {"proj": {"path": str(proj_dir), "execution": "remote"}},
        }
        config_file = tmp_path / "config.yaml"
        with open(config_file, "w") as f:
            yaml.dump(config_data, f)

        # Write a minimal manifest
        manifest_path = str(tmp_path / "manifest.yaml")
        manifest = {
            "toolchains": [],
            "utilities": [],
            "manual": {"apt": [], "npm_global": [], "pip": []},
        }
        with open(manifest_path, "w") as f:
            yaml.dump(manifest, f)

        mock_run = MagicMock()
        mock_run.return_value = MagicMock(returncode=0)

        args = argparse.Namespace(
            config=str(config_file),
            scan=False,
            no_push=True,
            no_cache=False,
        )
        with (
            patch("agent_forge.cli.MANIFEST_PATH", manifest_path),
            patch("agent_forge.remote_image_builder.subprocess.run", mock_run),
        ):
            cmd_remote_build_image(args)

        # Should have called docker build but not docker push
        calls = mock_run.call_args_list
        assert any("build" in str(c) for c in calls)
        assert not any("push" in str(c) for c in calls)

    def test_build_with_scan(self, tmp_path, capsys):
        from agent_forge.cli import cmd_remote_build_image

        proj_dir = tmp_path / "repo"
        proj_dir.mkdir()
        (proj_dir / ".git").mkdir()
        (proj_dir / "requirements.txt").touch()

        config_data = {
            "remote": {"docker_context": "vm", "image": "test:latest"},
            "projects": {"proj": {"path": str(proj_dir), "execution": "remote"}},
        }
        config_file = tmp_path / "config.yaml"
        with open(config_file, "w") as f:
            yaml.dump(config_data, f)

        manifest_path = str(tmp_path / "manifest.yaml")

        mock_run = MagicMock()
        mock_run.return_value = MagicMock(returncode=0)

        args = argparse.Namespace(
            config=str(config_file),
            scan=True,
            no_push=True,
            no_cache=False,
        )
        with (
            patch("agent_forge.cli.MANIFEST_PATH", manifest_path),
            patch("agent_forge.remote_image_builder.subprocess.run", mock_run),
        ):
            cmd_remote_build_image(args)

        # Manifest should have been written by the scan step
        assert Path(manifest_path).exists()
        loaded = yaml.safe_load(Path(manifest_path).read_text())
        assert any(t["name"] == "python" for t in loaded["toolchains"])
