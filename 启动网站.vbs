Set shell = CreateObject("WScript.Shell")
folder = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
shell.Run Chr(34) & folder & "\.venv\Scripts\pythonw.exe" & Chr(34) & " " & Chr(34) & folder & "\app.py" & Chr(34), 0, False
shell.Run "http://127.0.0.1:5000/", 1, False
