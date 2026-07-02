@echo off
setlocal

set "PROJECT_DIR=%~dp0"
cd /d "%PROJECT_DIR%"

echo Configuring HyperMesh background batch executable...
echo Project directory:
echo   %PROJECT_DIR%
echo.

powershell -NoProfile -ExecutionPolicy Bypass -File "%PROJECT_DIR%configure_hmbatch_path.ps1"
set "CODE=%ERRORLEVEL%"

echo.
if "%CODE%"=="0" (
    echo Done. Re-source the HyperMesh panel or restart HyperMesh, then try background meshing again.
) else (
    echo Failed. Please find hmbatch.exe manually and write its full path into:
    echo   %PROJECT_DIR%hypermesh_batch_path.txt
)
echo.
pause
exit /b %CODE%
