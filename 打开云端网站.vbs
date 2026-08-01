Option Explicit

Dim shell, fso, scriptDir, psScript, command, exitCode
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
psScript = fso.BuildPath(scriptDir, "scripts\open_private_cloud.ps1")

If Not fso.FileExists(psScript) Then
    MsgBox "没有找到私密访问程序：" & psScript, 16, "无法连接云端网站"
    WScript.Quit 1
End If

command = "powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File """ & psScript & """"
exitCode = shell.Run(command, 0, True)
If exitCode <> 0 Then
    MsgBox "私密连接没有建立成功，请检查访问配置、网络和本机访问密钥。", 16, "无法连接云端网站"
End If
