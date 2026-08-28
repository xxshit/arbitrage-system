Option Explicit

Dim shell, fileSystem, scriptDirectory, repositoryRoot, logDirectory
Dim backupScript, logFile, innerCommand, command, exitCode

Set shell = CreateObject("WScript.Shell")
Set fileSystem = CreateObject("Scripting.FileSystemObject")

scriptDirectory = fileSystem.GetParentFolderName(WScript.ScriptFullName)
repositoryRoot = fileSystem.GetParentFolderName(scriptDirectory)
logDirectory = fileSystem.BuildPath(repositoryRoot, "logs")
If Not fileSystem.FolderExists(logDirectory) Then
    fileSystem.CreateFolder(logDirectory)
End If

backupScript = fileSystem.BuildPath(scriptDirectory, "pull_cloud_mysql_backup.ps1")
logFile = fileSystem.BuildPath(logDirectory, "cloud-backup-task.log")

innerCommand = "powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File " _
    & QuoteArgument(backupScript) & " >> " & QuoteArgument(logFile) & " 2>&1"
command = shell.ExpandEnvironmentStrings("%ComSpec%") & " /d /s /c " & QuoteArgument(innerCommand)

' Window style 0 keeps the scheduled backup invisible; True waits so Task Scheduler records the real exit code.
exitCode = shell.Run(command, 0, True)
WScript.Quit exitCode

Function QuoteArgument(value)
    QuoteArgument = Chr(34) & Replace(value, Chr(34), Chr(34) & Chr(34)) & Chr(34)
End Function
