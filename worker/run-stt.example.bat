@echo off
rem Jarvis GPU STT server (Windows). Copy to run-stt.bat, fill in the token.
rem Auto-start at logon: create JarvisSTT.vbs in shell:startup containing:
rem   CreateObject("Wscript.Shell").Run "C:\path\to\run-stt.bat", 0, False
set JARVIS_STT_TOKEN=YOUR_JARVIS_HUD_TOKEN
set JARVIS_STT_MODEL=large-v3-turbo
cd /d %~dp0
.venv\Scripts\python.exe stt_server.py >> stt_server.log 2>&1
