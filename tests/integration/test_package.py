import os
from pathlib import Path
import subprocess
import sys

import pytest

from clipboard_typer import __version__


def run_python(tmp_path, *args):
    return subprocess.run(
        [sys.executable, *args], cwd=tmp_path,
        env=dict(os.environ, PYTHONUTF8="1"),
        capture_output=True, text=True, encoding="utf-8", timeout=15,
    )


@pytest.mark.parametrize("option", ["--help", "--version"])
def test_module_entrypoint_outside_repository(tmp_path, option):
    result = run_python(tmp_path, "-m", "clipboard_typer", option)
    assert result.returncode == 0, result.stderr
    assert __version__ in result.stdout if option == "--version" else "validate-config" in result.stdout


def test_validate_config_outside_repository(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("{}", encoding="utf-8")
    result = run_python(tmp_path, "-m", "clipboard_typer", "validate-config", str(path))
    assert result.returncode == 0, result.stderr
    assert "配置有效" in result.stdout


def test_console_script_entrypoint(tmp_path):
    import sysconfig
    name = "clipboard-typer.exe" if sys.platform == "win32" else "clipboard-typer"
    executable = Path(sysconfig.get_path("scripts")) / name
    result = subprocess.run([str(executable), "--version"], cwd=tmp_path,
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert __version__ in result.stdout


def test_public_api_and_help_do_not_initialize_native_ui(tmp_path):
    result = run_python(tmp_path, "-c", """
import sys
import clipboard_typer
from clipboard_typer.cli import main
assert clipboard_typer.validate_settings({})['version'] == 1
assert 'clipboard_typer.services.application' not in sys.modules
assert 'clipboard_typer.platforms.windows' not in sys.modules
assert 'tkinter' not in sys.modules
try:
    main(['--help'])
except SystemExit as exc:
    assert exc.code == 0
assert 'clipboard_typer.gui' not in sys.modules
""")
    assert result.returncode == 0, result.stderr
