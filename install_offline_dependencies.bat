@echo off
setlocal

set "PROJECT_DIR=%~dp0"
cd /d "%PROJECT_DIR%"

set "PYTHON_CMD="

py -3.12 --version >nul 2>nul
if not errorlevel 1 set "PYTHON_CMD=py -3.12"

if "%PYTHON_CMD%"=="" (
    python --version >nul 2>nul
    if not errorlevel 1 set "PYTHON_CMD=python"
)

if "%PYTHON_CMD%"=="" (
    echo ERROR: Python was not found.
    echo Please install Python 3.12 64-bit first.
    pause
    exit /b 1
)

echo Project directory:
echo   %PROJECT_DIR%
echo.
echo Python command:
echo   %PYTHON_CMD%
echo.

if not exist "requirements.txt" (
    echo ERROR: requirements.txt not found in %PROJECT_DIR%
    pause
    exit /b 1
)

if not exist "wheels\" (
    echo ERROR: wheels folder not found in %PROJECT_DIR%
    pause
    exit /b 1
)

%PYTHON_CMD% --version
%PYTHON_CMD% -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 12) else 1)"
if errorlevel 1 (
    echo.
    echo ERROR: This offline wheels folder is prepared for Python 3.12.
    echo Please install Python 3.12 64-bit, then run this bat again.
    pause
    exit /b 1
)

echo.
echo Installing offline dependencies from wheels...
%PYTHON_CMD% -m pip install --no-index --find-links wheels -r requirements.txt
if errorlevel 1 (
    echo.
    echo ERROR: Offline dependency installation failed.
    pause
    exit /b 1
)

echo.
echo Verifying mcp import...
%PYTHON_CMD% -c "import mcp; print('mcp OK')"
if errorlevel 1 (
    echo.
    echo ERROR: mcp import failed after installation.
    pause
    exit /b 1
)

echo.
echo Done. You can reopen or re-source the HyperMesh panel now.
pause
