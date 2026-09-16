"""PyInstaller entry point. The build adds src to its module search path."""
from clipboard_typer.gui import main

if __name__ == "__main__":
    raise SystemExit(main())
