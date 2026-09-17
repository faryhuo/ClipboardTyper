@echo off
setlocal EnableExtensions DisableDelayedExpansion
title Build ClipboardTyper EXE
set "CT_MODE=onefile"
set "CT_CLEAN="
set "CT_REFRESH="
set "CT_NO_PAUSE="
set "CT_HELP="
set "CT_BAD_ARG="

:parse_args
if "%~1"=="" goto args_ready
if /i "%~1"=="--onefile" (
    set "CT_MODE=onefile"
) else if /i "%~1"=="--onedir" (
    set "CT_MODE=onedir"
) else if /i "%~1"=="--clean" (
    set "CT_CLEAN=--clean"
) else if /i "%~1"=="--refresh-deps" (
    set "CT_REFRESH=--refresh-deps"
) else if /i "%~1"=="--no-pause" (
    set "CT_NO_PAUSE=1"
) else if /i "%~1"=="--help" (
    set "CT_HELP=1"
) else (
    set "CT_BAD_ARG=1"
)
shift
goto parse_args

:args_ready
if defined CT_BAD_ARG goto invalid_args
if defined CT_HELP goto help
pushd "%~dp0"
if errorlevel 1 goto directory_error

if not exist "src\clipboard_typer\gui.py" goto missing_source
if not exist "scripts\run_gui.py" goto missing_source
if not exist "scripts\build_exe.py" goto missing_source
if not exist "pyproject.toml" goto missing_source
if not exist "settings.json" goto missing_settings

echo Preparing PyInstaller...
if exist ".venv-build\Scripts\python.exe" goto check_venv

py -3 -c "import sys; sys.exit(0 if sys.platform == 'win32' and sys.version_info >= (3,9) else 1)" >nul 2>&1
if not errorlevel 1 goto use_py_launcher

python -c "import sys; sys.exit(0 if sys.platform == 'win32' and sys.version_info >= (3,9) else 1)" >nul 2>&1
if not errorlevel 1 goto use_python

python3 -c "import sys; sys.exit(0 if sys.platform == 'win32' and sys.version_info >= (3,9) else 1)" >nul 2>&1
if not errorlevel 1 goto use_python3

echo ERROR: Windows Python 3.9 or newer was not found.
echo Install Python from https://www.python.org/downloads/windows/
echo Enable the Python launcher or add Python to PATH, then run this file again.
goto failed

:use_py_launcher
set "CT_PYTHON_COMMAND=py -3"
goto create_venv

:use_python
set "CT_PYTHON_COMMAND=python"
goto create_venv

:use_python3
set "CT_PYTHON_COMMAND=python3"

:create_venv
echo Creating the local build environment...
%CT_PYTHON_COMMAND% -m venv ".venv-build"
if errorlevel 1 goto failed

:check_venv
".venv-build\Scripts\python.exe" -c "import sys; sys.exit(0 if sys.platform == 'win32' and sys.version_info >= (3,9) else 1)"
if errorlevel 1 goto broken_venv

".venv-build\Scripts\python.exe" -c "import tkinter; import _tkinter"
if errorlevel 1 goto missing_tk

echo.
echo Close any running ClipboardTyper EXE before rebuilding.
".venv-build\Scripts\python.exe" "scripts\build_exe.py" --mode %CT_MODE% %CT_CLEAN% %CT_REFRESH%
if errorlevel 1 goto failed
echo Run the EXE; right-click its tray icon to open Settings, hide the status card, or exit.
popd
if defined CT_NO_PAUSE exit /b 0
pause
exit /b 0

:missing_source
echo ERROR: Keep src, scripts and pyproject.toml beside build_exe.bat.
goto failed

:missing_tk
echo ERROR: This Python installation does not include Tcl/Tk.
echo Modify your Python installation and enable Tcl/Tk and IDLE, then rebuild.
goto failed

:missing_settings
echo ERROR: Put settings.json in the same folder as build_exe.bat.
goto failed

:broken_venv
echo ERROR: The local .venv-build environment cannot run.
echo Remove only the .venv-build folder and run build_exe.bat again.
goto failed

:failed
echo.
echo BUILD FAILED: See the error above.
echo If an older EXE is running, right-click its tray icon and exit before rebuilding.
popd
if defined CT_NO_PAUSE exit /b 1
pause
exit /b 1

:directory_error
echo ERROR: Cannot open the folder containing this BAT file.
if defined CT_NO_PAUSE exit /b 1
pause
exit /b 1

:help
call :usage
if defined CT_NO_PAUSE exit /b 0
pause
exit /b 0

:invalid_args
echo ERROR: Unknown build option.
call :usage
if defined CT_NO_PAUSE exit /b 1
pause
exit /b 1

:usage
echo Usage: build_exe.bat [--onefile ^| --onedir] [--clean] [--refresh-deps] [--no-pause]
echo   --onefile       Single EXE at dist\ClipboardTyper.exe [default].
echo   --onedir        Faster startup; distribute all of dist\ClipboardTyper\.
echo   --clean         Rebuild PyInstaller analysis instead of reusing its cache.
echo   --refresh-deps  Reinstall build dependencies even if unchanged.
echo   --no-pause      Exit without waiting for a key; useful for automation.
echo   --help          Show this help.
exit /b 0
