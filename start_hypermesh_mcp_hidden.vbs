' HyperMesh MCP SSE Server - silent background launcher
' Double-click to start the server with no visible window

Dim WshShell, strCommand
Set WshShell = CreateObject("Wscript.Shell")

' Set environment variables
Dim env
Set env = WshShell.Environment("Process")
If env("HYPERMESH_BATCH_EXE") = "" Then
    env("HYPERMESH_BATCH_EXE") = "C:\Program Files\Altair\2020\hwdesktop\hm\bin\win64\hmbatch.exe"
End If

' Run Python server hidden (0 = hidden window, False = don't wait)
strCommand = "python """ & CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName) & "\hypermesh_mcp_server.py"" --transport sse --host 127.0.0.1 --port 8742"
WshShell.Run strCommand, 0, False

Set WshShell = Nothing
