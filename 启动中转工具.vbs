Set shell = CreateObject("WScript.Shell")
shell.CurrentDirectory = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
shell.Run """" & shell.CurrentDirectory & "\.venv\Scripts\pythonw.exe"" """ & shell.CurrentDirectory & "\window_app.py""", 0, False
