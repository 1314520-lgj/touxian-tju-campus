@echo off
rem Re-login the WeRead account that feeds the public-account channel.
rem QR code is written next to this script and opened with the default viewer.
cd /d "%~dp0"
"%~dp0runtime\node\node.exe" "%~dp0login.mjs"
pause
