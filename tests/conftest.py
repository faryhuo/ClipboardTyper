"""Shared fixtures; tests use the installed package without sys.path changes."""
import copy

import pytest

from clipboard_typer.core.config import DEFAULT_SETTINGS


@pytest.fixture
def settings():
    return copy.deepcopy(DEFAULT_SETTINGS)


def pytest_collection_modifyitems(items):
    for item in items:
        if "integration" in item.path.parts:
            item.add_marker(pytest.mark.integration)
