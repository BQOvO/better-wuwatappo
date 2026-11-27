' Launch the watcher (tappo_watch.py) without a console window.
' The watcher pulls the CDP engine up whenever WuwaTappo is running.
' All paths derive from this script's own folder, so the folder can be moved.
'
' WHY ELEVATION IS REQUIRED
' If WuwaTappo runs elevated (High integrity) and this helper runs as a
' normal user (Medium), Windows UIPI silently refuses SetWindowRgn on
' Tappo's window. The engine then cannot clear Tappo's window clip region,
' so keycap bubbles get cut in half and eventually vanish as soon as the
' region covers the pet area. The same UIPI boundary is why keyboard
' polling stops seeing keys while an elevated game holds focus.
' So: re-launch ourselves elevated (UAC prompt) before starting anything.
'
' NOTE: this file is intentionally ASCII-only. cscript reads .vbs as ANSI
' unless it carries a BOM, so raw UTF-8 Chinese would break the parser.
Option Explicit

Dim fso, sh, here, pyw, flag, i

Set fso = CreateObject("Scripting.FileSystemObject")
Set sh  = CreateObject("WScript.Shell")
here = fso.GetParentFolderName(WScript.ScriptFullName)

' ---- elevation gate -------------------------------------------------------
' The elevated copy is started with /elevated so we do not loop forever.
Dim alreadyElevated
alreadyElevated = False
For i = 0 To WScript.Arguments.Count - 1
    If LCase(WScript.Arguments(i)) = "/elevated" Then alreadyElevated = True
Next

If Not alreadyElevated Then
    If Not CanWriteHklm() Then
        Dim rc
        rc = Elevate()
        If rc = 0 Then
            WScript.Quit 0          ' elevated copy took over
        End If
        ' UAC declined or unavailable: carry on degraded, but say so once.
        MsgBox "Administrator rights were not granted." & vbCrLf & vbCrLf & _
               "Keycap bubbles will be clipped whenever WuwaTappo runs as " & _
               "administrator, and keys may not register while an elevated " & _
               "game has focus." & vbCrLf & vbCrLf & _
               "To fix it: right-click this file and choose " & _
               """Run as administrator""." & vbCrLf & vbCrLf & _
               "Starting anyway...", 48, "WuwaTappo helper"
    End If
End If

' ---- start the watcher ----------------------------------------------------
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

' 0 = hidden window, False = do not wait.
sh.Run """" & pyw & """ """ & here & "\tappo_watch.py""", 0, False

' ---------- helpers --------------------------------------------------------

' Are we elevated? Try writing a throwaway key under HKLM.
' Reading HKLM is allowed for everyone, writing is not: a clean yes/no probe.
Function CanWriteHklm()
    Dim reg, key
    CanWriteHklm = False
    key = "SOFTWARE\WuwaTappoHelper\Probe"
    On Error Resume Next
    Set reg = GetObject("winmgmts:\\.\root\default:StdRegProv")
    If Err.Number <> 0 Then
        Err.Clear
        On Error GoTo 0
        Exit Function
    End If
    reg.DeleteKey &H80000002, "SOFTWARE\WuwaTappoHelper"
    Err.Clear
    reg.CreateKey &H80000002, key, 0
    If Err.Number = 0 Then
        CanWriteHklm = True
        reg.DeleteKey &H80000002, "SOFTWARE\WuwaTappoHelper"
    End If
    Err.Clear
    On Error GoTo 0
End Function

' Start an elevated copy of this script. Returns 0 when it was launched.
Function Elevate()
    Dim app, cmd, n
    Elevate = 1
    On Error Resume Next
    Set app = CreateObject("Shell.Application")
    If Err.Number <> 0 Then
        Err.Clear
        On Error GoTo 0
        Exit Function
    End If
    cmd = """" & WScript.ScriptFullName & """ /elevated"
    n = app.ShellExecute("wscript.exe", cmd, "", "runas", 1)
    If Err.Number <> 0 Then
        Elevate = Err.Number      ' user said No to UAC (usually 0x800704C7)
        Err.Clear
        On Error GoTo 0
        Exit Function
    End If
    If n > 32 Then Elevate = 0
    On Error GoTo 0
End Function

' ---------- locate pythonw: PATH first, then the usual install dirs ----------
Function FindPythonw()
    Dim ex, line, cand, j, dirs
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
    For j = 0 To UBound(dirs)
        cand = dirs(j) & "\pythonw.exe"
        If fso.FileExists(cand) Then
            FindPythonw = cand
            Exit Function
        End If
    Next
End Function
