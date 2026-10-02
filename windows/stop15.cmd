@echo off
rem lars15 stop - kills listener on 8646; close the worker window too (or use PS one-liner)
setlocal
for /f "tokens=5" %%i in ('netstat -ano ^| findstr /r ":8646 .*LISTENING"') do taskkill /PID %%i /F >nul 2>&1
echo lars15 server stopped. Close the lars15-worker window if still open.
