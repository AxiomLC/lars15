@echo off
rem lars15 start - one server + agent worker
setlocal
set ROOT=%~dp0..
echo [lars15] starting main server :8646 + agent worker (LiveKit)...

start "lars15-server" cmd /k "cd /d %ROOT%\server && %ROOT%\.venv\Scripts\python.exe server15.py"
start "lars15-worker" cmd /k "cd /d %ROOT% && %ROOT%\.venv\Scripts\python.exe server\agent_worker.py dev"

echo Two windows launched: lars15-server + lars15-worker.
echo HUD: http://localhost:8646/
