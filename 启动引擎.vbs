' Launch the watcher (tappo_watch.py) without a console window.
' The watcher pulls the CDP engine up whenever WuwaTappo is running.
' All paths derive from this script's own folder, so the folder can be moved.
'
' NOTE: this file is intentionally ASCII-only. cscript reads .vbs as ANSI
' unless it carries a BOM, so raw UTF-8 Chinese would break the parser.
Option Explicit

Dim fso, sh, here, pyw, flag

Set fso = CreateObject("Scripting.FileSystemObject")
Set sh  = CreateObject("WScript.Shell")
here = fso.GetParentFolderName(WScript.ScriptFullName)

pyw = FindPythonw()
If pyw = "" Then
    MsgBox "pythonw.exe not found." & vbCrLf & vbCrLf & _
           "Install Python 3.8+ and tick ""Add Python to PATH"".", _
           16, "WuwaTappo helper"
    WScript.Quit 1
End If

' Drop the "stopped" marker left by the stop script, otherwise the watcher
' thinks you asked it to stay down and refuses to pull the engine.
' The folder name spells out in Chinese; built from code points to stay ASCII.
flag = sh.ExpandEnvironmentStrings("%LOCALAPPDATA%") & "\" _
     & ChrW(40483) & ChrW(28526) & ChrW(23453) & ChrW(31665) _
     & ChrW(21161) & ChrW(25163) & "CDP\engine.stopped"
If fso.FileExists(flag) Then fso.DeleteFile flag

sh.Run """" & pyw & """ """ & here & "\tappo_watch.py""", 0, False

' ---------- locate pythonw: PATH first, then the usual install dirs ----------
Function FindPythonw()
    Dim ex, line, cand, i, dirs
    FindPythonw = ""

    On Error Resume Next
    Set ex = sh.Exec("cmd /c where pythonw.exe")
    If Err.Number = 0 Then
        line = ex.StdOut.ReadLine()
        If Len(line) > 0 Then FindPythonw = Trim(line)
    End If
    Err.Clear
    On Error GoTo 0
    If FindPythonw <> "" Then Exit Function

    dirs = Array( _
        sh.ExpandEnvironmentStrings("%LOCALAPPDATA%\Programs\Python\Python314"), _
        sh.ExpandEnvironmentStrings("%LOCALAPPDATA%\Programs\Python\Python313"), _
        sh.ExpandEnvironmentStrings("%LOCALAPPDATA%\Programs\Python\Python312"), _
        sh.ExpandEnvironmentStrings("%LOCALAPPDATA%\Programs\Python\Python311"), _
        sh.ExpandEnvironmentStrings("%ProgramFiles%\Python314"), _
        sh.ExpandEnvironmentStrings("%ProgramFiles%\Python313"), _
        "C:\Python314", "C:\Python313", "C:\Python312")
    For i = 0 To UBound(dirs)
        cand = dirs(i) & "\pythonw.exe"
        If fso.FileExists(cand) Then
            FindPythonw = cand
            Exit Function
        End If
    Next
End Function
