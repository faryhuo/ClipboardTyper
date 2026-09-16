"""Public, platform-independent configuration API for ClipboardTyper."""
from clipboard_typer.core.config import ConfigError, read_settings, validate_settings

__version__ = "0.1.0"
__all__ = ["ConfigError", "read_settings", "validate_settings", "__version__"]
