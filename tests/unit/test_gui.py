import sys

import pytest

from clipboard_typer import gui


@pytest.mark.skipif(sys.platform != "win32", reason="Tk packaging is Windows-specific")
def test_settings_native_module_can_be_preloaded_without_creating_a_window():
    gui._preload_settings_ui()

    assert "_tkinter" in sys.modules
    assert "tkinter" in sys.modules
    assert "tkinter.ttk" in sys.modules
