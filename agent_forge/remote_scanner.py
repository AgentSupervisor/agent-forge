"""Toolchain scanner — detects project toolchains for Docker image generation."""

from __future__ import annotations

import datetime
import logging
from pathlib import Path

import yaml

from .config import ForgeConfig, ProjectConfig

logger = logging.getLogger(__name__)


# Mapping: marker file (or glob) -> toolchain definition
TOOLCHAIN_MARKERS: list[dict] = [
    {
        "markers": ["requirements.txt", "pyproject.toml", "Pipfile", "setup.py"],
        "name": "python",
        "packages_apt": ["python3", "python3-pip", "python3-venv"],
    },
    {
        "markers": ["Gemfile"],
        "name": "ruby",
        "packages_apt": ["ruby", "ruby-bundler"],
    },
    {
        "markers": ["go.mod"],
        "name": "go",
        "packages_apt": ["golang"],
    },
    {
        "markers": ["Cargo.toml"],
        "name": "rust",
        "packages_apt": [],
        "install_script": "curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y",
    },
    {
        "markers": ["pom.xml", "build.gradle", "build.gradle.kts"],
        "name": "java",
        "packages_apt": ["openjdk-17-jdk", "maven"],
    },
    {
        "markers": ["Makefile", "CMakeLists.txt"],
        "name": "c-cpp",
        "packages_apt": ["build-essential", "cmake"],
    },
    {
        "markers": ["docker-compose.yml", "docker-compose.yaml"],
        "name": "docker-cli",
        "packages_apt": ["docker.io"],
    },
]

# Utilities to check on the local machine
UTILITY_ALLOWLIST: dict[str, list[str]] = {
    "jq": ["jq"],
    "ripgrep": ["ripgrep"],
    "fd": ["fd-find"],
    "wget": ["wget"],
    "tree": ["tree"],
    "sqlite3": ["sqlite3"],
    "zip": ["zip", "unzip"],
    "htop": ["htop"],
}


def scan_project(project_path: str) -> dict:
    """Scan a single project for toolchain markers.

    Returns dict with keys: markers (list[str]), toolchains (list[dict]).
    """
    path = Path(project_path)
    found_markers: list[str] = []
    found_toolchains: list[dict] = []

    if not path.is_dir():
        logger.warning("Project path does not exist: %s", project_path)
        return {"markers": [], "toolchains": []}

    for tc in TOOLCHAIN_MARKERS:
        for marker in tc["markers"]:
            if (path / marker).exists():
                found_markers.append(marker)
                tc_entry = {
                    "name": tc["name"],
                    "packages_apt": list(tc["packages_apt"]),
                }
                if "install_script" in tc:
                    tc_entry["install_script"] = tc["install_script"]
                # Avoid duplicates
                if not any(t["name"] == tc["name"] for t in found_toolchains):
                    found_toolchains.append(tc_entry)
                break  # One marker per toolchain is enough

    return {"markers": found_markers, "toolchains": found_toolchains}


def scan_local_utilities(check_bin_fn=None) -> list[dict]:
    """Check which allowlisted utilities are installed locally.

    Args:
        check_bin_fn: Optional callable(name) -> bool for testing.
            Defaults to checking if the binary exists via shutil.which.
    """
    import shutil

    if check_bin_fn is None:
        def check_bin_fn(name: str) -> bool:
            return shutil.which(name) is not None

    found: list[dict] = []
    for name, apt_packages in UTILITY_ALLOWLIST.items():
        if check_bin_fn(name):
            found.append({"name": name, "packages_apt": list(apt_packages)})
    return found


def scan_all_projects(
    config: ForgeConfig,
    include_local_utils: bool = True,
    check_bin_fn=None,
) -> dict:
    """Scan all remote-execution projects and produce a toolchain manifest.

    Args:
        config: Loaded ForgeConfig.
        include_local_utils: Whether to scan local machine for utilities.
        check_bin_fn: Override for utility binary check (for testing).

    Returns:
        Manifest dict ready to be written as YAML.
    """
    scanned_projects: list[dict] = []
    all_toolchains: dict[str, dict] = {}

    for name, project in config.projects.items():
        entry: dict = {
            "name": name,
            "path": project.path,
            "execution": project.execution,
        }

        if project.execution == "local":
            entry["skipped"] = True
            entry["reason"] = "execution: local"
            scanned_projects.append(entry)
            continue

        result = scan_project(project.path)
        entry["markers"] = result["markers"]
        scanned_projects.append(entry)

        for tc in result["toolchains"]:
            if tc["name"] not in all_toolchains:
                all_toolchains[tc["name"]] = tc

    utilities: list[dict] = []
    if include_local_utils:
        utilities = scan_local_utilities(check_bin_fn=check_bin_fn)

    manifest = {
        "generated_at": datetime.datetime.now(datetime.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
        "scanned_projects": scanned_projects,
        "toolchains": list(all_toolchains.values()),
        "utilities": utilities,
        "manual": {"apt": [], "npm_global": [], "pip": []},
    }
    return manifest


def load_manifest(manifest_path: str) -> dict | None:
    """Load an existing toolchain manifest from disk."""
    path = Path(manifest_path)
    if not path.exists():
        return None
    with open(path) as f:
        return yaml.safe_load(f) or None


def save_manifest(manifest: dict, manifest_path: str) -> None:
    """Write toolchain manifest to disk."""
    path = Path(manifest_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        yaml.dump(manifest, f, default_flow_style=False, sort_keys=False)
    logger.info("Manifest written to %s", manifest_path)
