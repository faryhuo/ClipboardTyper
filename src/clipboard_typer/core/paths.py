"""Resolve runtime data without writing into an installed Python package."""
import json
import os
from pathlib import Path
import sys

from clipboard_typer.core.config import DEFAULT_SETTINGS


def application_paths(settings_path=None):
    """Use an explicit file, portable/source settings, or the user data directory.

    CLIPBOARD_TYPER_CONFIG overrides the default location. Relative overrides
    are resolved from the working directory; default paths never depend on it.
    """
    data_dir = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))) / "ClipboardTyper"
    data_dir.mkdir(parents=True, exist_ok=True)
    override = settings_path if settings_path is not None else os.environ.get("CLIPBOARD_TYPER_CONFIG")
    if override is not None:
        selected = Path(override).expanduser().resolve()
    else:
        source_root = Path(__file__).resolve().parents[3]
        if getattr(sys, "frozen", False):
            base = Path(sys.executable).resolve().parent
        elif (source_root / "pyproject.toml").is_file() and (source_root / "src" / "clipboard_typer").is_dir():
            base = source_root
        else:
            base = data_dir
        selected = base / "settings.json"

    if not selected.exists():
        content = json.dumps(DEFAULT_SETTINGS, ensure_ascii=False, indent=2) + "\n"
        try:
            with selected.open("x", encoding="utf-8") as stream:
                stream.write(content)
        except FileExistsError:
            pass
        except OSError:
            if override is not None:
                raise
            selected = data_dir / "settings.json"
            try:
                with selected.open("x", encoding="utf-8") as stream:
                    stream.write(content)
            except FileExistsError:
                pass
    return selected, data_dir / "clipboard_typer.log"
