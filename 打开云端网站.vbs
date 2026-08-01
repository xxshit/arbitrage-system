Option Explicit

Dim shell, fso, keyPath, sshPath, command, url
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

sshPath = "C:\Windows\System32\OpenSSH\ssh.exe"
keyPath = shell.ExpandEnvironmentStrings("%USERPROFILE%") & "\.ssh\arbitrage_deploy_5_61_208_92"
url = "http://127.0.0.1:5000"

If Not fso.FileExists(sshPath) Then
    MsgBox "没有找到 Windows OpenSSH，请先安装 OpenSSH 客户端。", 16, "无法连接云端网站"
    WScript.Quit 1
End If

If Not fso.FileExists(keyPath) Then
    MsgBox "没有找到云端部署密钥：" & keyPath, 16, "无法连接云端网站"
    WScript.Quit 1
End If

command = """" & sshPath & """ -N -i """ & keyPath & """ -p 16206" & _
          " -o BatchMode=yes -o ExitOnForwardFailure=yes" & _
          " -o ServerAliveInterval=60 -o ServerAliveCountMax=3" & _
          " -L 127.0.0.1:5000:127.0.0.1:15831 root@5.61.208.92"

' 隐藏启动；如果连接已经存在，新进程会自动退出，不影响现有隧道。
shell.Run command, 0, False
WScript.Sleep 2500
shell.Run url, 1, False

