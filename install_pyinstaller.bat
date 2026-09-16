@echo off
setlocal EnableExtensions DisableDelayedExpansion
title Install PyInstaller - ClipboardTyper
pushd "%~dp0"
if errorlevel 1 goto directory_error

echo Installing PyInstaller for ClipboardTyper...
echo.

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
echo [1/3] Creating the local build environment...
%CT_PYTHON_COMMAND% -m venv ".venv-build"
if errorlevel 1 goto failed

:check_venv
".venv-build\Scripts\python.exe" -c "import sys; sys.exit(0 if sys.platform == 'win32' and sys.version_info >= (3,9) else 1)"
if errorlevel 1 goto broken_venv

echo [2/3] Checking pip...
".venv-build\Scripts\python.exe" -m pip --version >nul 2>&1
if not errorlevel 1 goto install_package
".venv-build\Scripts\python.exe" -m ensurepip --upgrade
if errorlevel 1 goto failed

:install_package
echo [3/3] Installing build dependencies from pyproject.toml...
".venv-build\Scripts\python.exe" -m pip install -e ".[exe]"
if errorlevel 1 goto failed

".venv-build\Scripts\python.exe" -m PyInstaller --version
if errorlevel 1 goto failed
echo.
echo SUCCESS: PyInstaller is ready.
echo To create the EXE, double-click build_exe.bat.
popd
if /i "%~1"=="--no-pause" exit /b 0
pause
exit /b 0

:broken_venv
echo ERROR: The local .venv-build environment cannot run.
echo Remove only the .venv-build folder and run this installer again.
goto failed

:failed
echo.
echo FAILED: See the error above. Package downloads require internet access.
popd
if /i "%~1"=="--no-pause" exit /b 1
pause
exit /b 1

:directory_error
echo ERROR: Cannot open the folder containing this BAT file.
if /i "%~1"=="--no-pause" exit /b 1
pause
exit /b 1
