@echo off
rem Launcher entry (kept ASCII-only: cmd.exe parses .bat with the console code page,
rem and a UTF-8 Chinese filename would be mangled on a GBK console).
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0launcher.ps1"
