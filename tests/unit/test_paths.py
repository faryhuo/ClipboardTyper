from pathlib import Path

import pytest

from clipboard_typer.core import paths
from clipboard_typer.core.config import read_settings


@pytest.fixture(autouse=True)
def isolated_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "data"))
    monkeypatch.delenv("CLIPBOARD_TYPER_CONFIG", raising=False)
    monkeypatch.delattr(paths.sys, "frozen", raising=False)


def test_source_settings_do_not_depend_on_working_directory(tmp_path, monkeypatch):
    project = tmp_path / "project"
    module = project / "src/clipboard_typer/core/paths.py"
    module.parent.mkdir(parents=True)
    (project / "pyproject.toml").touch()
    monkeypatch.setattr(paths, "__file__", str(module))
    monkeypatch.chdir(tmp_path)
    settings_path, log_path = paths.application_paths()
    assert settings_path == project / "settings.json"
    assert log_path == tmp_path / "data/ClipboardTyper/clipboard_typer.log"
    assert read_settings(settings_path)["version"] == 1


def test_installed_package_uses_user_data(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "__file__", str(tmp_path / "venv/Lib/site-packages/clipboard_typer/core/paths.py"))
    settings_path, _ = paths.application_paths()
    assert settings_path == tmp_path / "data/ClipboardTyper/settings.json"


def test_frozen_executable_preserves_portable_configuration(tmp_path, monkeypatch):
    portable = tmp_path / "portable"
    portable.mkdir()
    settings_path = portable / "settings.json"
    settings_path.write_text('{"profiles":{"fast":{"chunk":24}}}', encoding="utf-8")
    before = settings_path.read_bytes()
    monkeypatch.setattr(paths.sys, "frozen", True, raising=False)
    monkeypatch.setattr(paths.sys, "executable", str(portable / "ClipboardTyper.exe"))
    assert paths.application_paths()[0] == settings_path
    assert settings_path.read_bytes() == before


def test_explicit_path_takes_precedence_over_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("CLIPBOARD_TYPER_CONFIG", str(tmp_path / "environment.json"))
    chosen = tmp_path / "chosen.json"
    assert paths.application_paths(chosen)[0] == chosen
    assert not (tmp_path / "environment.json").exists()


def test_environment_path(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CLIPBOARD_TYPER_CONFIG", "custom.json")
    assert paths.application_paths()[0] == tmp_path / "custom.json"


def test_explicit_missing_directory_does_not_silently_fallback(tmp_path):
    with pytest.raises(OSError):
        paths.application_paths(tmp_path / "missing/settings.json")
    assert not (tmp_path / "data/ClipboardTyper/settings.json").exists()


def test_read_only_portable_directory_falls_back(tmp_path, monkeypatch):
    portable = tmp_path / "portable"
    portable.mkdir()
    monkeypatch.setattr(paths.sys, "frozen", True, raising=False)
    monkeypatch.setattr(paths.sys, "executable", str(portable / "ClipboardTyper.exe"))
    original_open = Path.open

    def open_file(path, *args, **kwargs):
        if path.parent == portable:
            raise PermissionError("read-only")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", open_file)
    assert paths.application_paths()[0] == tmp_path / "data/ClipboardTyper/settings.json"
