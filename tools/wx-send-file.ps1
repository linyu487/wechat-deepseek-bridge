param(
    [Parameter(Mandatory = $true)][string]$TextFile,
    [Parameter(Mandatory = $true)][string]$To
)

$ErrorActionPreference = 'Stop'
if (-not (Test-Path -LiteralPath $TextFile)) { Write-Error "text file not found: $TextFile"; exit 1 }

# Measure idle time BEFORE waking the window: if nobody has touched the machine
# for a while, turn the monitor back off after sending so the bot does not keep
# the screen lit (synthetic input from SendKeys resets the idle timer).
$idleAtStart = 0
try {
Add-Type @"
using System;
using System.Runtime.InteropServices;
public class IdleChk {
  [StructLayout(LayoutKind.Sequential)]
  public struct LASTINPUTINFO { public uint cbSize; public uint dwTime; }
  [DllImport("user32.dll")] public static extern bool GetLastInputInfo(ref LASTINPUTINFO lii);
  [DllImport("kernel32.dll")] public static extern uint GetTickCount();
  [DllImport("user32.dll")]
  public static extern IntPtr SendMessageTimeout(IntPtr hWnd, uint Msg, IntPtr wParam,
      IntPtr lParam, uint fuFlags, uint uTimeout, out IntPtr result);
  public static double IdleSeconds() {
    LASTINPUTINFO l = new LASTINPUTINFO();
    l.cbSize = (uint)Marshal.SizeOf(l);
    if (!GetLastInputInfo(ref l)) return 0;
    return (GetTickCount() - l.dwTime) / 1000.0;
  }
  public static void BlankScreen() {
    IntPtr r;
    SendMessageTimeout((IntPtr)0xFFFF, 0x0112, (IntPtr)0xF170, (IntPtr)2, 2, 1000, out r);
  }
  public static void MonitorOn() {
    IntPtr r;
    SendMessageTimeout((IntPtr)0xFFFF, 0x0112, (IntPtr)0xF170, (IntPtr)(-1), 2, 1000, out r);
  }
  [DllImport("shell32.dll")]
  public static extern int SHQueryUserNotificationState(out int state);
  public static bool InFullscreen() {
    int s = 0;
    if (SHQueryUserNotificationState(out s) != 0) return false;
    return s == 3 || s == 4;  // D3D fullscreen / presentation mode
  }
}
"@
$idleAtStart = [IdleChk]::IdleSeconds()
} catch { }

# UI automation needs an active display: wake the monitor first, otherwise
# SetForegroundWindow fails while the screen is off.
try { [IdleChk]::MonitorOn() } catch { }
Start-Sleep -Milliseconds 700

# Start the black-screen keeper (single instance, never takes focus).
try {
  Start-Process -FilePath "CHANGE_ME_PYTHON_DIR\pythonw.exe" `
    -ArgumentList "$PSScriptRoot\wx-blackscreen.py" -WindowStyle Hidden
} catch { }

Add-Type @"
using System;
using System.Text;
using System.Runtime.InteropServices;
public class WxWin2 {
  public delegate bool EnumProc(IntPtr hWnd, IntPtr lParam);
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc cb, IntPtr lp);
  [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetWindowText(IntPtr h, StringBuilder s, int n);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetClassName(IntPtr h, StringBuilder s, int n);
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int c);
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
  [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
  [DllImport("user32.dll")] public static extern bool BringWindowToTop(IntPtr h);
  [DllImport("user32.dll")] public static extern bool AttachThreadInput(uint a, uint b, bool f);
  [DllImport("user32.dll")] public static extern bool SetWindowPos(IntPtr h, IntPtr after, int x, int y, int cx, int cy, uint flags);
  [DllImport("kernel32.dll")] public static extern uint GetCurrentThreadId();
}
"@

# --- locate the main WeChat window (even when hidden to the tray) ---
$wxPids = @(Get-Process -Name Weixin, WeChat -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Id)
$script:found = [IntPtr]::Zero
$cb = [WxWin2+EnumProc]{
    param($h, $l)
    $p = 0
    [WxWin2]::GetWindowThreadProcessId($h, [ref]$p) | Out-Null
    if ($wxPids -contains [int]$p) {
        $cls = New-Object System.Text.StringBuilder 128
        [WxWin2]::GetClassName($h, $cls, 128) | Out-Null
        if ($cls.ToString() -eq 'Qt51514QWindowIcon') {
            $ttl = New-Object System.Text.StringBuilder 128
            [WxWin2]::GetWindowText($h, $ttl, 128) | Out-Null
            $t = $ttl.ToString()
            if ($t -ne '' -and $t -ne 'Weixin') { $script:found = $h; return $false }
        }
    }
    return $true
}
[WxWin2]::EnumWindows($cb, [IntPtr]::Zero) | Out-Null

$h = $script:found
if ($h -eq [IntPtr]::Zero) {
    $proc = Get-Process -Name Weixin, WeChat -ErrorAction SilentlyContinue |
            Where-Object { $_.MainWindowHandle -ne 0 } | Select-Object -First 1
    if ($proc) { $h = $proc.MainWindowHandle }
}
if ($h -eq [IntPtr]::Zero) { Write-Error "WeChat main window not found"; exit 2 }

# --- restore + force to foreground ---
[WxWin2]::ShowWindow($h, 9) | Out-Null   # SW_RESTORE
[WxWin2]::ShowWindow($h, 5) | Out-Null   # SW_SHOW
[WxWin2]::BringWindowToTop($h) | Out-Null
Start-Sleep -Milliseconds 300

$ok = $false
for ($i = 0; $i -lt 12; $i++) {
    [WxWin2]::SetForegroundWindow($h) | Out-Null
    Start-Sleep -Milliseconds 200
    if ([WxWin2]::GetForegroundWindow() -eq $h) { $ok = $true; break }
    $fg = [WxWin2]::GetForegroundWindow()
    $opid = 0
    $ft = [WxWin2]::GetWindowThreadProcessId($fg, [ref]$opid)
    $ct = [WxWin2]::GetCurrentThreadId()
    [WxWin2]::AttachThreadInput($ct, $ft, $true) | Out-Null
    [WxWin2]::BringWindowToTop($h) | Out-Null
    [WxWin2]::SetForegroundWindow($h) | Out-Null
    [WxWin2]::AttachThreadInput($ct, $ft, $false) | Out-Null
    Start-Sleep -Milliseconds 200
    if ([WxWin2]::GetForegroundWindow() -eq $h) { $ok = $true; break }
}
if (-not $ok) { Write-Error "could not bring WeChat to foreground"; exit 3 }

Start-Sleep -Milliseconds 500
$text = [System.IO.File]::ReadAllText($TextFile, [System.Text.Encoding]::UTF8)
$marker = Join-Path $PSScriptRoot "..\tmp\blackscreen.sending"
New-Item -ItemType File -Path $marker -Force | Out-Null
$first = ($text -split "`r?`n")[0]
if ($first -match '^@@FILE:(.+)$') {
    $filePath = $Matches[1].Trim()
    & "$PSScriptRoot\wx-ui-send.ps1" -To $To -File $filePath
} else {
    & "$PSScriptRoot\wx-ui-send.ps1" -To $To -Text $text
}
$rc = $LASTEXITCODE
Remove-Item $marker -Force -ErrorAction SilentlyContinue
exit $rc
