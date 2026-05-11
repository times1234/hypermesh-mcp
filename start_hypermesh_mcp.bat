@echo off
set "PROJECT_DIR=%~dp0"
if not defined HYPERMESH_BATCH_EXE set "HYPERMESH_BATCH_EXE=C:\Program Files\Altair\2020\hwdesktop\hm\bin\win64\hmbatch.exe"
if not defined HYPERMESH_GUI_EXE set "HYPERMESH_GUI_EXE=C:\Program Files\Altair\2020\hwdesktop\hw\bin\win64\hw.exe"
python "%PROJECT_DIR%hypermesh_mcp_server.py"
