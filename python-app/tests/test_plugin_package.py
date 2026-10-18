from __future__ import annotations

import json
import os
import runpy
import shutil
import stat
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
import yaml
from jsonschema import Draft202012Validator

REPOSITORY = Path(__file__).parents[2]
SCRIPT = REPOSITORY / "scripts/package_plugin.py"
packager = runpy.run_path(str(SCRIPT))
build_archive = packager["build_archive"]


@pytest.fixture
def source(tmp_path: Path) -> Path:
    root = tmp_path / "source without git"
    for relative in packager["source_files"](REPOSITORY):
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPOSITORY / relative, target)
    return root


def test_upload_archive_is_complete_and_excludes_local_state(source: Path, tmp_path: Path) -> None:
    excluded = [
        ".diff",
        "upstream.tar.gz",
        ".env",
        ".env.local",
        "config.json",
        ".git/config",
        "dist/old.zip",
        "python-app/.venv/secret.py",
        "python-app/app/config.json",
        "python-app/tests/local.py",
        "python-app/app/backends/__pycache__/bad.py",
        "python-app/app/remote/.cache/secret.py",
        "python-app/app/remote/node_modules/local.py",
        "skills/remote-computer/credentials.json",
        "console/private.txt",
        "labs/local.txt",
    ]
    for relative in excluded:
        path = source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("LOCAL-SECRET-MARKER", encoding="utf-8")
    output = build_archive(source, tmp_path / "upload.zip")
    with zipfile.ZipFile(output) as archive:
        names = archive.namelist()
        assert names == sorted(set(names))
        assert {
            "plugin.json",
            "mcp.json",
            "LICENSE",
            "LICENSES/OpenAI-CUA-Sample-App-LICENSE",
            "docs/configuration.md",
            "docs/upstream/openai-sample-README.md",
            "python-app/pyproject.toml",
            "python-app/uv.lock",
            "python-app/app/mcp_server.py",
            "python-app/app/remote/worker.py",
            "python-app/app/remote/release_inputs.py",
            "python-app/app/backends/rdp/aardwolf.py",
            "python-app/app/backends/vnc/remote_control.py",
            "skills/remote-computer/SKILL.md",
        } <= set(names)
        assert not set(names).intersection(excluded)
        assert not any(name.startswith(("/", "../", "scripts/", "python-app/tests/")) for name in names)
        assert all(b"LOCAL-SECRET-MARKER" not in archive.read(name) for name in names)
        for component in ("plugin", "mcp"):
            schema = json.loads(
                (REPOSITORY / f"python-app/tests/fixtures/agent_plugins_1_0_0_{component}.schema.json").read_text()
            )
            Draft202012Validator(schema).validate(json.loads(archive.read(f"{component}.json")))
        skill = archive.read("skills/remote-computer/SKILL.md").decode("utf-8")
        metadata = yaml.safe_load(skill.split("---", 2)[1])
        assert metadata["name"] == "remote-computer" and metadata["description"]
        assert archive.testzip() is None


def test_archive_is_repeatable_despite_source_timestamps(source: Path, tmp_path: Path) -> None:
    first = build_archive(source, tmp_path / "first.zip").read_bytes()
    for relative in packager["source_files"](source):
        os.utime(source / relative, (1_700_000_000, 1_700_000_000))
    second = build_archive(source, tmp_path / "second.zip")
    assert second.read_bytes() == first
    with zipfile.ZipFile(second) as archive:
        for entry in archive.infolist():
            assert entry.date_time == (1980, 1, 1, 0, 0, 0)
            assert stat.S_IMODE(entry.external_attr >> 16) == 0o644


def test_cli_needs_only_standard_library_and_resolves_its_own_root(source: Path, tmp_path: Path) -> None:
    script = source / "scripts/package_plugin.py"
    script.parent.mkdir()
    shutil.copyfile(SCRIPT, script)
    result = subprocess.run(
        [sys.executable, "-I", "-S", str(script)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    )
    expected = source / "dist/remote-computer-use-1.0.0.zip"
    assert result.stdout.strip() == str(expected)
    assert expected.is_file()
    assert not (source / ".git").exists() and not (source / "python-app/.venv").exists()


@pytest.mark.parametrize("missing", ["mcp.json", "python-app/uv.lock", "python-app/app/remote/worker.py"])
def test_missing_runtime_files_fail_before_writing_archive(source: Path, tmp_path: Path, missing: str) -> None:
    (source / missing).unlink()
    output = tmp_path / "plugin.zip"
    with pytest.raises(ValueError, match="Missing"):
        build_archive(source, output)
    assert not output.exists()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("name", "../escape"),
        ("name", "a..b"),
        ("name", "a--b"),
        ("version", "../escape"),
        ("version", "1.0"),
        ("version", "1.0.0-01"),
        ("version", "1.0.0-.."),
    ],
)
def test_invalid_package_identity_is_rejected(source: Path, tmp_path: Path, field: str, value: str) -> None:
    path = source / "plugin.json"
    manifest = json.loads(path.read_text())
    manifest[field] = value
    path.write_text(json.dumps(manifest), encoding="utf-8")
    output = tmp_path / "plugin.zip"
    with pytest.raises(ValueError, match="plugin.json"):
        build_archive(source, output)
    assert not output.exists()


def test_failure_preserves_existing_archive_and_removes_partial_file(source: Path, tmp_path: Path, monkeypatch) -> None:
    output = tmp_path / "plugin.zip"
    output.write_bytes(b"previous archive")

    def fail(*args, **kwargs):
        raise OSError("simulated disk failure")

    monkeypatch.setattr(zipfile.ZipFile, "writestr", fail)
    with pytest.raises(OSError, match="simulated"):
        build_archive(source, output)
    assert output.read_bytes() == b"previous archive"
    assert not list(tmp_path.glob(".plugin.zip.*.tmp"))


def test_unreadable_runtime_directory_cannot_be_silently_skipped(source: Path, tmp_path: Path, monkeypatch) -> None:
    def fail_walk(*args, onerror, **kwargs):
        onerror(PermissionError("unreadable runtime directory"))
        return iter(())

    monkeypatch.setattr(os, "walk", fail_walk)
    with pytest.raises(PermissionError, match="unreadable"):
        build_archive(source, tmp_path / "plugin.zip")


def test_output_cannot_overwrite_a_source_manifest(source: Path) -> None:
    manifest = source / "plugin.json"
    before = manifest.read_bytes()
    with pytest.raises(ValueError, match=".zip"):
        build_archive(source, manifest)
    assert manifest.read_bytes() == before


@pytest.mark.parametrize("relative", ["README.md", "python-app/app/backends/external"])
def test_source_links_cannot_include_files_outside_project(source: Path, tmp_path: Path, relative: str) -> None:
    external = tmp_path / "external"
    external.mkdir()
    (external / "secret.py").write_text("LOCAL-SECRET-MARKER", encoding="utf-8")
    link = source / relative
    directory = not link.exists()
    if link.is_file():
        link.unlink()
    try:
        link.symlink_to(external if directory else external / "secret.py", target_is_directory=directory)
    except OSError:
        if not directory or os.name != "nt":
            pytest.skip("Creating symbolic links requires OS permission")
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(external)],
            stdin=subprocess.DEVNULL, capture_output=True, check=False,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        if result.returncode:
            pytest.skip("Creating a directory junction requires OS permission")
    try:
        with pytest.raises(ValueError, match="links"):
            build_archive(source, tmp_path / "plugin.zip")
    finally:
        if link.is_junction():
            link.rmdir()
    assert (external / "secret.py").is_file()
