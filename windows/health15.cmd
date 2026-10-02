@echo off
setlocal
set OK=1
netstat -ano | findstr /r ":8646 .*LISTENING" >nul 2>&1
if errorlevel 1 (echo [MISS] main server :8646 & set OK=0) else echo [ OK ] main server :8646
curl -s -o /dev/null http://127.0.0.1:8646/api/health
if errorlevel 1 (echo [MISS] /api/health & set OK=0) else echo [ OK ] API health responds
if "%OK%"=="1" (echo HEALTH: ALL GOOD) else (echo HEALTH: PROBLEMS & exit /b 1)
