@echo off
setlocal

if "%~1"=="" goto usage

powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0split_clipboard_text.ps1" %*
exit /b %ERRORLEVEL%

:usage
echo Usage: %~nx0 INPUT_TEXT_FILE [OUTPUT_DIRECTORY] [-Force]
echo.
echo Recreates files from ClipboardTyper Base64 transfer text.
exit /b 2
