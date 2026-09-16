import sys
from unittest.mock import Mock

import pytest

from clipboard_typer.cli import main


def test_run_passes_explicit_config_to_gui(monkeypatch, tmp_path):
    start = Mock(return_value=0)
    monkeypatch.setattr("clipboard_typer.gui.main", start)
    config = tmp_path / "settings.json"
    assert main(["run", "--config", str(config)]) == 0
    start.assert_called_once_with(settings_path=config)


def test_default_command_starts_gui(monkeypatch):
    start = Mock(return_value=0)
    monkeypatch.setattr("clipboard_typer.gui.main", start)
    assert main([]) == 0
    start.assert_called_once_with(settings_path=None)


def test_non_windows_gui_returns_actionable_error(monkeypatch, capsys):
    from clipboard_typer.gui import main as gui_main
    monkeypatch.setattr(sys, "platform", "linux")
    assert gui_main() == 1
    assert "Windows" in capsys.readouterr().out


@pytest.mark.parametrize("content", ["not json", '{"options":{"show_popup":1}}'])
def test_invalid_config_has_nonzero_exit(tmp_path, capsys, content):
    path = tmp_path / "invalid.json"
    path.write_text(content, encoding="utf-8")
    assert main(["validate-config", str(path)]) == 1
    assert "配置无效" in capsys.readouterr().err
