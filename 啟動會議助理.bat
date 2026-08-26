@echo off
setlocal EnableExtensions EnableDelayedExpansion

cd /d "%~dp0"
title Meeting Assistant

echo ==================================================
echo Meeting Assistant one-click launcher
echo ==================================================
echo.

if /I "%~1"=="--check" (
    call :resolve_python
    if errorlevel 1 exit /b 1
    echo Check OK: !PYTHON_EXE!
    exit /b 0
)

if not exist ".env" (
    if exist ".env.example" (
        echo [INFO] .env not found. Creating it from .env.example.
        copy ".env.example" ".env" >nul
        echo [WARN] Please confirm API keys in .env before production use.
        echo.
    ) else (
        echo [WARN] .env not found, and .env.example is missing.
        echo.
    )
)

call :ensure_venv
if errorlevel 1 goto fail

set "PYTHON_EXE=%CD%\.venv\Scripts\python.exe"

echo [1/3] Upgrading pip...
"%PYTHON_EXE%" -m pip install --upgrade pip
if errorlevel 1 goto fail

echo.
echo [2/3] Installing required packages...
"%PYTHON_EXE%" -m pip install -r requirements.txt
if errorlevel 1 goto fail

echo.
echo [3/3] Starting Meeting Assistant...
echo.
echo Local page will open automatically:
echo   http://127.0.0.1:8001/history
echo.
echo Keep this window open while using the system.
echo Press Ctrl+C in this window to stop the server.
echo.

"%PYTHON_EXE%" start.py
set "EXIT_CODE=%ERRORLEVEL%"

echo.
echo Meeting Assistant stopped. Exit code: %EXIT_CODE%
pause
exit /b %EXIT_CODE%

:ensure_venv
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -c "import sys" >nul 2>nul
    if not errorlevel 1 exit /b 0

    echo [WARN] Existing .venv is unusable, likely because the project was moved from another computer or Windows account.
    call :backup_broken_venv
    if errorlevel 1 exit /b 1
) else if exist ".venv" (
    echo [WARN] Existing .venv is incomplete and will be rebuilt.
    call :backup_broken_venv
    if errorlevel 1 exit /b 1
)

echo [INFO] .venv not found. Creating a virtual environment...
call :create_venv
if errorlevel 1 (
    echo [ERROR] Could not create .venv. Please install Python 3.13 or newer.
    exit /b 1
)
exit /b 0

:backup_broken_venv
if not exist ".venv" exit /b 0

for /f "usebackq delims=" %%T in (`powershell -NoProfile -Command "Get-Date -Format yyyyMMdd-HHmmss"`) do set "VENV_TIMESTAMP=%%T"
if not defined VENV_TIMESTAMP set "VENV_TIMESTAMP=!RANDOM!"
set "VENV_BACKUP=.venv.broken-!VENV_TIMESTAMP!"
if exist "!VENV_BACKUP!" set "VENV_BACKUP=!VENV_BACKUP!-!RANDOM!"

move ".venv" "!VENV_BACKUP!" >nul
if errorlevel 1 (
    echo [ERROR] Could not preserve the unusable .venv directory.
    exit /b 1
)

echo [INFO] Preserved unusable environment as !VENV_BACKUP!
exit /b 0

:create_venv
where py >nul 2>nul
if not errorlevel 1 (
    py -3.14 -m venv .venv >nul 2>nul
    if exist ".venv\Scripts\python.exe" exit /b 0
    py -3.13 -m venv .venv >nul 2>nul
    if exist ".venv\Scripts\python.exe" exit /b 0
    py -3 -m venv .venv >nul 2>nul
    if exist ".venv\Scripts\python.exe" exit /b 0
)

where python >nul 2>nul
if not errorlevel 1 (
    python -m venv .venv >nul 2>nul
    if exist ".venv\Scripts\python.exe" exit /b 0
)

exit /b 1

:resolve_python
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -c "import sys" >nul 2>nul
    if not errorlevel 1 (
        set "PYTHON_EXE=%CD%\.venv\Scripts\python.exe"
        exit /b 0
    )
)

where py >nul 2>nul
if not errorlevel 1 (
    for /f "usebackq delims=" %%P in (`py -3.14 -c "import sys; print(sys.executable)" 2^>nul`) do (
        set "PYTHON_EXE=%%P"
        exit /b 0
    )
    for /f "usebackq delims=" %%P in (`py -3.13 -c "import sys; print(sys.executable)" 2^>nul`) do (
        set "PYTHON_EXE=%%P"
        exit /b 0
    )
    for /f "usebackq delims=" %%P in (`py -3 -c "import sys; print(sys.executable)" 2^>nul`) do (
        set "PYTHON_EXE=%%P"
        exit /b 0
    )
)

where python >nul 2>nul
if not errorlevel 1 (
    for /f "usebackq delims=" %%P in (`python -c "import sys; print(sys.executable)" 2^>nul`) do (
        set "PYTHON_EXE=%%P"
        exit /b 0
    )
)

echo [ERROR] Python was not found.
exit /b 1

:fail
echo.
echo [ERROR] Startup failed.
echo Please check the messages above.
pause
exit /b 1
