"""Build the bundled Codex plugin ZIP using only the Python standard library."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import stat
import tempfile
import zipfile
from pathlib import Path

# Package the MCP component, not the development Console, Labs or test suite.
FILES = (
    "plugin.json",
    "mcp.json",
    "LICENSE",
    "README.md",
    "README_zh.md",
    "docs/upstream-compatibility.md",
    "docs/release-compatibility.md",
    "skills/remote-computer/SKILL.md",
    "python-app/pyproject.toml",
    "python-app/uv.lock",
    "python-app/app/__init__.py",
    "python-app/app/actions.py",
    "python-app/app/computer_session.py",
    "python-app/app/input_tracking.py",
    "python-app/app/mcp_server.py",
    "python-app/app/plugin_session.py",
    "python-app/app/processes.py",
    "python-app/app/profiles.py",
)
PYTHON_TREES = ("python-app/app/backends", "python-app/app/remote")
REQUIRED_MODULES = (
    "python-app/app/backends/__init__.py",
    "python-app/app/remote/__init__.py",
    "python-app/app/remote/worker.py",
    "python-app/app/remote/release_inputs.py",
)
IGNORED_DIRECTORIES = {
    "__pycache__",
    "node_modules",
    "env",
    "data",
    "dist",
    "coverage",
    "test-artifacts",
}


def contained_file(root: Path, relative: str) -> Path:
    path = root
    for part in Path(relative).parts:
        path /= part
        if path.is_symlink() or path.is_junction():
            raise ValueError(f"Package source must not contain links: {relative}")
    if not path.resolve().is_relative_to(root) or not path.is_file():
        raise ValueError(f"Missing or unsafe package source: {relative}")
    return path


def _walk_failed(error: OSError) -> None:
    raise error


def source_files(root: Path) -> list[str]:
    """Select shipped components before reading any source file contents."""
    selected = set(FILES)
    for tree in PYTHON_TREES:
        contained_file(root, f"{tree}/__init__.py")
        for directory, folders, filenames in os.walk(root / tree, onerror=_walk_failed):
            folders[:] = sorted(
                name
                for name in folders
                if not name.startswith(".") and name not in IGNORED_DIRECTORIES
            )
            for name in folders:
                path = Path(directory) / name
                if path.is_symlink() or path.is_junction():
                    raise ValueError(
                        f"Package source must not contain links: {path.relative_to(root)}"
                    )
            for name in filenames:
                if name.endswith(".py") and not name.startswith("."):
                    selected.add((Path(directory) / name).relative_to(root).as_posix())
    for relative in selected | set(REQUIRED_MODULES):
        contained_file(root, relative)
    return sorted(selected)


def build_archive(root: Path, output: Path | None = None) -> Path:
    root = root.resolve()
    files = source_files(root)
    manifests = {}
    for component in ("plugin", "mcp"):
        manifest = json.loads((root / f"{component}.json").read_text(encoding="utf-8"))
        if (
            not isinstance(manifest, dict)
            or manifest.get("$schema")
            != f"https://agent-plugins.org/schemas/1.0.0/{component}.schema.json"
        ):
            raise ValueError(
                f"{component}.json must declare its Agent Plugins 1.0.0 schema"
            )
        manifests[component] = manifest
    name, version = manifests["plugin"].get("name"), manifests["plugin"].get("version")
    if (
        not isinstance(name, str)
        or not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]{0,62}[a-z0-9])?", name)
        or "--" in name
        or ".." in name
    ):
        raise ValueError("plugin.json needs a valid package name")
    number = r"(?:0|[1-9][0-9]*)"
    identifier = rf"(?:{number}|[0-9]*[A-Za-z-][0-9A-Za-z-]*)"
    if not isinstance(version, str) or not re.fullmatch(
        rf"{number}\.{number}\.{number}(?:-{identifier}(?:\.{identifier})*)?(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?",
        version,
    ):
        raise ValueError("plugin.json needs a semantic version")
    if (
        not isinstance(manifests["mcp"].get("mcpServers"), dict)
        or not manifests["mcp"]["mcpServers"]
    ):
        raise ValueError("mcp.json needs a bundled MCP server")
    output = (output or root / "dist" / f"{name}-{version}.zip").absolute()
    if output.suffix.lower() != ".zip" or output.is_symlink() or output.is_junction():
        raise ValueError("Output must be a regular .zip file path")
    output.parent.mkdir(parents=True, exist_ok=True)
    # Build outside the staging tree and replace the destination only on success.
    with tempfile.TemporaryDirectory(
        prefix="remote-computer-use-package-"
    ) as temporary:
        staging = Path(temporary) / "plugin"
        for relative in files:
            destination = staging / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(contained_file(root, relative), destination)
        with tempfile.NamedTemporaryFile(
            prefix=f".{output.name}.", suffix=".tmp", dir=output.parent, delete=False
        ) as stream:
            pending = Path(stream.name)
        try:
            with zipfile.ZipFile(pending, "w") as archive:
                for relative in files:
                    info = zipfile.ZipInfo(relative, date_time=(1980, 1, 1, 0, 0, 0))
                    info.create_system = 3
                    info.external_attr = (stat.S_IFREG | 0o644) << 16
                    archive.writestr(
                        info,
                        (staging / relative).read_bytes(),
                        compress_type=zipfile.ZIP_DEFLATED,
                        compresslevel=9,
                    )
            with zipfile.ZipFile(pending) as archive:
                if archive.namelist() != files or archive.testzip() is not None:
                    raise ValueError("ZIP content verification failed")
            os.replace(pending, output)
        finally:
            pending.unlink(missing_ok=True)
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        help="ZIP destination (default: dist/<plugin-name>-<version>.zip)",
    )
    arguments = parser.parse_args()
    try:
        output = build_archive(Path(__file__).resolve().parents[1], arguments.output)
    except (OSError, ValueError, zipfile.BadZipFile) as error:
        parser.exit(1, f"Packaging failed: {error}\n")
    print(output)


if __name__ == "__main__":
    main()
