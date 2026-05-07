@echo off
set HYPERMESH_BATCH_EXE=E:\Program Files\Altair\2020\hwdesktop\hm\bin\win64\hmbatch.exe
echo Starting HyperMesh MCP Server in SSE mode on http://127.0.0.1:8742/sse
echo Keep this window open while using Cowork.
echo Press Ctrl+C to stop.
echo.
python "E:\mcp\hypermesh-mcp-server\hypermesh_mcp_server.py" --transport sse --host 127.0.0.1 --port 8742
pause
