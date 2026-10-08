import base64
import os
import subprocess
from pathlib import Path

import pytest

from clipboard_typer.services.typing_service import BASE64_LINE_LENGTH, FILE_BEGIN_PREFIX, FILE_BEGIN_SUFFIX, FILE_END, TEXT_BEGIN_PREFIX


@pytest.mark.skipif(os.name != "nt", reason="BAT splitter requires Windows PowerShell")
def test_bat_splitter_restores_text_and_binary_files(tmp_path):
    files = {
        "notes.txt": ("中文\r\ntext" * 20).encode("utf-8"),
        "image.png": b"\x89PNG\r\n\x1a\n\x00\xff\x10",
        "empty.dat": b"",
    }
    blocks = []
    for name, data in files.items():
        payload = base64.b64encode(data).decode("ascii")
        payload = "\n".join(
            payload[offset:offset + BASE64_LINE_LENGTH]
            for offset in range(0, len(payload), BASE64_LINE_LENGTH)
        )
        blocks.append(
            f"{FILE_BEGIN_PREFIX}{name}{FILE_BEGIN_SUFFIX}\n"
            f"{payload}\n{FILE_END}"
        )
    typed = {"script.py": "中文\r\n\r\nprint(1)\r\n", "blank.txt": ""}
    for name, text in typed.items():
        blocks.append(f"{TEXT_BEGIN_PREFIX}{name}{FILE_BEGIN_SUFFIX}\n{text}\n{FILE_END}")
        files[name] = text.encode()  # Line endings are kept as received.
    bundle = tmp_path / "received.txt"
    bundle.write_bytes("\n".join(blocks).encode())
    output = tmp_path / "restored"
    script = Path(__file__).resolve().parents[2] / "scripts" / "split_clipboard_text.bat"
    command = subprocess.list2cmdline([str(script), str(bundle), str(output)])

    result = subprocess.run(
        ["cmd.exe", "/d", "/s", "/c", command],
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert {path.name: path.read_bytes() for path in output.iterdir()} == files
