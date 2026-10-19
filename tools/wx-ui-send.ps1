# 微信界面自动化发送：wx-ui-send.ps1
#
# 用法：
#   powershell -ExecutionPolicy Bypass -File wx-ui-send.ps1 -To "鳞羽" -Text "你好"
#   powershell -ExecutionPolicy Bypass -File wx-ui-send.ps1 -To "鳞羽" -File "C:\path\a.png"
#
# 前提：微信已登录并开着窗口（可以不在最前，但不能最小化）。

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$To,
    [string]$Text,
    [string]$File
)

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms

Add-Type @"
using System;
using System.Runtime.InteropServices;
public class WxWin {
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
    [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int c);
    [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr h);
}
"@

$proc = Get-Process -Name 'Weixin', 'WeChat' -ErrorAction SilentlyContinue |
        Where-Object { $_.MainWindowHandle -ne 0 } | Select-Object -First 1
if (-not $proc) { Write-Host '找不到微信主窗口（是否最小化到托盘？）' -ForegroundColor Red; exit 2 }

$h = $proc.MainWindowHandle
if ([WxWin]::IsIconic($h)) { [WxWin]::ShowWindow($h, 9) | Out-Null }
[WxWin]::ShowWindow($h, 9) | Out-Null
[WxWin]::SetForegroundWindow($h) | Out-Null
Start-Sleep -Milliseconds 600
if ([WxWin]::GetForegroundWindow() -ne $h) { Write-Host '微信没能切到前台，可能被别的窗口挡住' -ForegroundColor Yellow }

# 1. 全局搜索，打开对应聊天
[System.Windows.Forms.SendKeys]::SendWait('^f')
Start-Sleep -Milliseconds 900
Set-Clipboard -Value $To
Start-Sleep -Milliseconds 300
[System.Windows.Forms.SendKeys]::SendWait('^v')
Start-Sleep -Milliseconds 1500
[System.Windows.Forms.SendKeys]::SendWait('{ENTER}')
Start-Sleep -Milliseconds 1800

# 2. 发内容
if ($File) {
    if (-not (Test-Path -LiteralPath $File)) { Write-Host "文件不存在: $File" -ForegroundColor Red; exit 1 }
    Set-Clipboard -Path (Resolve-Path -LiteralPath $File).Path
    Start-Sleep -Milliseconds 400
    [System.Windows.Forms.SendKeys]::SendWait('^v')
    Start-Sleep -Milliseconds 800
    [System.Windows.Forms.SendKeys]::SendWait('{ENTER}')
    Start-Sleep -Milliseconds 800
}
if ($Text) {
    Set-Clipboard -Value $Text
    Start-Sleep -Milliseconds 200
    [System.Windows.Forms.SendKeys]::SendWait('^v')
    Start-Sleep -Milliseconds 500
    [System.Windows.Forms.SendKeys]::SendWait('{ENTER}')
}

Write-Host "已发送给: $To"
