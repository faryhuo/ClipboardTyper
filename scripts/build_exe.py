"""Incremental Windows packaging, invoked with the local build environment."""
import argparse
import hashlib
import importlib.metadata
import json
from pathlib import Path
import shutil
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]


def run(*args, cwd):
    subprocess.run([sys.executable, *args], cwd=cwd, check=True)


def dependency_fingerprint(root):
    # Include installed versions so removed/changed tools invalidate the stamp.
    # Hash all project metadata, including build-system and editable install settings.
    return {
        "schema": 1,
        "project": hashlib.sha256((root / "pyproject.toml").read_bytes()).hexdigest(),
        "python": sys.version,
        "executable": str(Path(sys.executable).resolve()),
        "packages": sorted(
            (dist.metadata["Name"], dist.version)
            for dist in importlib.metadata.distributions()
            if dist.metadata["Name"]
        ),
    }


def prepare_dependencies(root, refresh=False):
    stamp = Path(sys.prefix) / ".clipboard-typer-build.json"
    # JSON normalizes tuples to lists, so compare serialized fingerprints.
    fingerprint = json.dumps(dependency_fingerprint(root), sort_keys=True)
    try:
        cached = stamp.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        cached = ""
    if not refresh and cached == fingerprint:
        try:
            run("-c", "import PyInstaller.__main__; import clipboard_typer", cwd=root)
        except subprocess.CalledProcessError:
            print("Build dependencies need repair.", flush=True)
        else:
            print("Build dependencies unchanged; skipping pip install.", flush=True)
            return

    # Never reuse an old success stamp after an interrupted or failed install.
    stamp.unlink(missing_ok=True)
    try:
        run("-m", "pip", "--version", cwd=root)
    except subprocess.CalledProcessError:
        run("-m", "ensurepip", "--upgrade", cwd=root)
    print("Installing build dependencies from pyproject.toml...", flush=True)
    run("-m", "pip", "install", "-e", ".[exe]", cwd=root)
    run("-m", "PyInstaller", "--version", cwd=root)
    # Save only after installation and validation both succeeded.
    stamp.write_text(json.dumps(dependency_fingerprint(root), sort_keys=True), encoding="utf-8")


def build(root, mode="onefile", clean=False, refresh_deps=False):
    prepare_dependencies(root, refresh=refresh_deps)
    run("-c", "import tkinter; import _tkinter", cwd=root)
    work = root / "build" / mode
    work.mkdir(parents=True, exist_ok=True)
    dist = root / "dist"
    # COLLECT replaces its entire output directory with --noconfirm. Stage onedir
    # builds away from the user's portable configuration before copying binaries.
    output = work / "staging" if mode == "onedir" else dist
    args = ["-m", "PyInstaller", "--noconfirm", "--" + mode]
    if clean:
        args.append("--clean")
    args.extend([
        "--windowed", "--noupx", "--name", "ClipboardTyper",
        "--icon", str(root / "src/clipboard_typer/assets/clipboard-typer.ico"),
        "--hidden-import", "_tkinter", "--hidden-import", "tkinter",
        "--hidden-import", "tkinter.ttk",
        "--collect-data", "clipboard_typer", "--distpath", str(output),
        "--workpath", str(work), "--specpath", str(work),
        "--paths", str(root / "src"), str(root / "scripts/run_gui.py"),
    ])
    print(f"Building {mode}; {'clean analysis' if clean else 'reusing build cache'}...", flush=True)
    run(*args, cwd=root)
    destination = dist / "ClipboardTyper" if mode == "onedir" else dist
    generated = output / "ClipboardTyper" if mode == "onedir" else output
    if not (generated / "ClipboardTyper.exe").is_file():
        raise RuntimeError("PyInstaller did not produce ClipboardTyper.exe")
    if mode == "onedir":
        shutil.copytree(generated, destination, dirs_exist_ok=True)
    config = destination / "settings.json"
    if not config.exists():
        shutil.copy2(root / "settings.json", config)
    print(f"\nSUCCESS: {destination / 'ClipboardTyper.exe'}")
    print(f"CONFIG: {config}")
    print("Existing runtime settings are preserved when rebuilding.")
    if mode == "onedir":
        print("Distribute the entire ClipboardTyper folder, including _internal and settings.json.")
    print("The target computer does not need Python or AutoHotkey.")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("onefile", "onedir"), default="onefile")
    parser.add_argument("--clean", action="store_true")
    parser.add_argument("--refresh-deps", action="store_true")
    args = parser.parse_args(argv)
    try:
        build(ROOT, args.mode, args.clean, args.refresh_deps)
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"BUILD FAILED: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
