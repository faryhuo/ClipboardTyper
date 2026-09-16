"""Plain-data events shared by the application and UI threads."""
from dataclasses import dataclass


@dataclass
class UiEvent:
    kind: str
    text: str = ""
    detail: str = ""
    data: object = None
