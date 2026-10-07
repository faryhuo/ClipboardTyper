"""Public, platform-independent configuration API for ClipboardTyper."""
from importlib.metadata import PackageNotFoundError, version as _distribution_version

from clipboard_typer.core.config import ConfigError, read_settings, validate_settings

try:
    # pyproject.toml is the single version source; EXE builds copy its metadata.
    __version__ = _distribution_version("clipboard-typer")
except PackageNotFoundError:
    __version__ = "0+unknown"
__all__ = ["ConfigError", "read_settings", "validate_settings", "__version__"]
