@echo off
setlocal EnableExtensions DisableDelayedExpansion
title Build ClipboardTyper EXE
pushd "%~dp0"
if errorlevel 1 goto directory_error

if not exist "src\clipboard_typer\gui.py" goto missing_source
if not exist "scripts\run_gui.py" goto missing_source
if not exist "pyproject.toml" goto missing_source
if not exist "install_pyinstaller.bat" goto missing_installer
if not exist "settings.json" goto missing_settings

echo Preparing PyInstaller...
call "%~dp0install_pyinstaller.bat" --no-pause
if errorlevel 1 goto failed

".venv-build\Scripts\python.exe" -c "import tkinter; import _tkinter"
if errorlevel 1 goto missing_tk

title Build ClipboardTyper EXE
echo.
echo Building the single-file Windows EXE with no console window...
echo Close any running dist\ClipboardTyper.exe before rebuilding.
if not exist "build" mkdir "build"
if errorlevel 1 goto failed

".venv-build\Scripts\python.exe" -m PyInstaller ^
    --noconfirm ^
    --clean ^
    --onefile ^
    --windowed ^
    --noupx ^
    --name "ClipboardTyper" ^
    --icon "%~dp0src\clipboard_typer\assets\clipboard-typer.ico" ^
    --collect-data "clipboard_typer" ^
    --distpath "dist" ^
    --workpath "build" ^
    --specpath "build" ^
    --paths "src" ^
    "scripts\run_gui.py"

if errorlevel 1 goto failed
if not exist "dist\ClipboardTyper.exe" goto failed

rem Keep the user's existing runtime configuration when rebuilding.
if exist "dist\settings.json" goto settings_ready
copy /y "settings.json" "dist\settings.json" >nul
if errorlevel 1 goto failed

:settings_ready

echo.
echo SUCCESS: "%CD%\dist\ClipboardTyper.exe"
echo CONFIG: "%CD%\dist\settings.json"
echo Existing dist\settings.json is preserved when rebuilding.
echo Run the EXE; right-click its tray icon to open Settings, hide the status card, or exit.
echo The target computer does not need Python or AutoHotkey.
popd
if /i "%~1"=="--no-pause" exit /b 0
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

:missing_installer
echo ERROR: Put install_pyinstaller.bat in the same folder as build_exe.bat.
goto failed

:failed
echo.
echo BUILD FAILED: See the error above.
echo If an older EXE is running, right-click its tray icon and exit before rebuilding.
popd
if /i "%~1"=="--no-pause" exit /b 1
pause
exit /b 1

:directory_error
echo ERROR: Cannot open the folder containing this BAT file.
if /i "%~1"=="--no-pause" exit /b 1
pause
exit /b 1
