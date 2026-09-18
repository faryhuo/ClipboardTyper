import importlib.util
from pathlib import Path
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


@pytest.fixture
def builder(tmp_path, monkeypatch):
    script = Path(__file__).resolve().parents[2] / "scripts/build_exe.py"
    spec = importlib.util.spec_from_file_location("build_exe", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    environment = tmp_path / "venv"
    environment.mkdir()
    monkeypatch.setattr(module.sys, "prefix", str(environment))
    monkeypatch.setattr(module.importlib.metadata, "distributions", lambda: [])
    monkeypatch.setattr(module, "run", Mock())
    return module


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "project with spaces"
    root.mkdir()
    (root / "pyproject.toml").write_text('[project]\nname = "clipboard-typer"\n', encoding="utf-8")
    (root / "settings.json").write_text('{"version": 1}', encoding="utf-8")
    return root


def installs(builder):
    return [call for call in builder.run.call_args_list if call.args[:3] == ("-m", "pip", "install")]


def test_source_edits_do_not_reinstall_unchanged_dependencies(builder, project):
    builder.prepare_dependencies(project)
    (project / "source.py").write_text("# source changed", encoding="utf-8")
    builder.prepare_dependencies(project)
    assert len(installs(builder)) == 1


@pytest.mark.parametrize("change", ["metadata", "environment", "refresh", "stamp", "python"])
def test_dependency_changes_invalidate_cache(builder, project, monkeypatch, change):
    builder.prepare_dependencies(project)
    if change == "metadata":
        (project / "pyproject.toml").write_text('[project]\nversion = "0.2.0"', encoding="utf-8")
    elif change == "environment":
        monkeypatch.setattr(builder.importlib.metadata, "distributions", lambda: [
            SimpleNamespace(metadata={"Name": "PyInstaller"}, version="6.99"),
        ])
    elif change == "stamp":
        (Path(builder.sys.prefix) / ".clipboard-typer-build.json").write_text("corrupt", encoding="utf-8")
    elif change == "python":
        monkeypatch.setattr(builder.sys, "version", "new interpreter")
    builder.prepare_dependencies(project, refresh=change == "refresh")
    assert len(installs(builder)) == 2


def test_failed_refresh_invalidates_previous_success_stamp(builder, project):
    builder.prepare_dependencies(project)

    def fail_install(*args, **kwargs):
        if args[:3] == ("-m", "pip", "install"):
            raise subprocess.CalledProcessError(1, args)

    builder.run.side_effect = fail_install
    with pytest.raises(subprocess.CalledProcessError):
        builder.prepare_dependencies(project, refresh=True)
    builder.run.reset_mock(side_effect=True)
    builder.prepare_dependencies(project)
    assert len(installs(builder)) == 1


def test_broken_import_repairs_unchanged_environment(builder, project):
    builder.prepare_dependencies(project)

    def fail_import(*args, **kwargs):
        if args[0] == "-c":
            raise subprocess.CalledProcessError(1, args)

    builder.run.side_effect = fail_import
    builder.prepare_dependencies(project)
    assert len(installs(builder)) == 2


@pytest.mark.parametrize("mode", ["onefile", "onedir"])
def test_rebuild_preserves_configuration_and_uses_separate_cache(builder, project, monkeypatch, mode):
    monkeypatch.setattr(builder, "prepare_dependencies", Mock())
    destination = project / "dist"
    if mode == "onedir":
        destination /= "ClipboardTyper"
    destination.mkdir(parents=True)
    custom = b'{"profiles":{"fast":{"chunk":24}}}\r\n'
    (destination / "settings.json").write_bytes(custom)

    def simulate_pyinstaller(*args, **kwargs):
        if args[:2] != ("-m", "PyInstaller"):
            return
        output = Path(args[args.index("--distpath") + 1])
        if mode == "onedir":
            assert output != project / "dist"
            output /= "ClipboardTyper"
            (output / "_internal").mkdir(parents=True, exist_ok=True)
            (output / "_internal/python.dll").write_bytes(b"runtime")
        output.mkdir(parents=True, exist_ok=True)
        (output / "ClipboardTyper.exe").write_bytes(b"new exe")

    builder.run.side_effect = simulate_pyinstaller
    builder.build(project, mode)
    builder.build(project, mode, clean=True)
    assert (destination / "settings.json").read_bytes() == custom
    assert (destination / "ClipboardTyper.exe").read_bytes() == b"new exe"
    if mode == "onedir":
        assert (destination / "_internal/python.dll").read_bytes() == b"runtime"
    builds = [call.args for call in builder.run.call_args_list if call.args[:2] == ("-m", "PyInstaller")]
    assert "--clean" not in builds[0]
    assert "--clean" in builds[1]
    for args in builds:
        hidden = [args[index + 1] for index, value in enumerate(args) if value == "--hidden-import"]
        assert hidden == ["_tkinter", "tkinter", "tkinter.ttk"]
    assert all(str(project / "build" / mode) == args[args.index("--workpath") + 1] for args in builds)


def test_failed_onedir_build_leaves_previous_distribution_intact(builder, project, monkeypatch):
    monkeypatch.setattr(builder, "prepare_dependencies", Mock())
    destination = project / "dist/ClipboardTyper"
    destination.mkdir(parents=True)
    (destination / "settings.json").write_bytes(b"original config")
    (destination / "ClipboardTyper.exe").write_bytes(b"original exe")
    def fail_packaging(*args, **kwargs):
        if args[:2] == ("-m", "PyInstaller"):
            raise subprocess.CalledProcessError(1, args)

    builder.run.side_effect = fail_packaging
    with pytest.raises(subprocess.CalledProcessError):
        builder.build(project, "onedir")
    assert (destination / "settings.json").read_bytes() == b"original config"
    assert (destination / "ClipboardTyper.exe").read_bytes() == b"original exe"
