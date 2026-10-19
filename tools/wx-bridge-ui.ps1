# 微信小号桥接控制台（WinForms）
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
Add-Type -AssemblyName Microsoft.VisualBasic
[System.Windows.Forms.Application]::EnableVisualStyles()

$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$py   = "CHANGE_ME_PYTHON_DIR\python.exe"
$pyw  = "CHANGE_ME_PYTHON_DIR\pythonw.exe"
$scriptPath = Join-Path $root "tools\wx-bridge.py"
$logPath    = Join-Path $root "tmp\bridge.log"
$lockPath   = Join-Path $root "tmp\bridge.lock"
$pausePath  = Join-Path $root "tmp\bridge.pause"
$stopPath   = Join-Path $root "tmp\bridge.stop"
$startupDir = [Environment]::GetFolderPath('Startup')
$startupVbs = Join-Path $startupDir "dsh-wx-bridge.vbs"

function Get-BridgeState {
    if (-not (Test-Path $lockPath)) { return "stopped" }
    try {
        $procId = [int](Get-Content $lockPath -ErrorAction Stop)
        $p = Get-Process -Id $procId -ErrorAction SilentlyContinue
        if ($null -eq $p) { return "stopped" }
        if (Test-Path $pausePath) { return "paused" }
        return "running"
    } catch { return "stopped" }
}

$form = New-Object System.Windows.Forms.Form
$form.Text = "微信桥接控制台"
$form.ClientSize = New-Object System.Drawing.Size(470, 360)
$form.StartPosition = "CenterScreen"
$form.FormBorderStyle = "FixedSingle"
$form.MaximizeBox = $false
$form.BackColor = [System.Drawing.Color]::White
$form.Font = New-Object System.Drawing.Font("Microsoft YaHei UI", 9)

$lblState = New-Object System.Windows.Forms.Label
$lblState.Location = New-Object System.Drawing.Point(18, 14)
$lblState.Size = New-Object System.Drawing.Size(434, 30)
$lblState.Font = New-Object System.Drawing.Font("Microsoft YaHei UI", 13, [System.Drawing.FontStyle]::Bold)
$lblState.Text = "● 检查中…"
$form.Controls.Add($lblState)

$lblLast = New-Object System.Windows.Forms.Label
$lblLast.Location = New-Object System.Drawing.Point(20, 48)
$lblLast.Size = New-Object System.Drawing.Size(432, 20)
$lblLast.ForeColor = [System.Drawing.Color]::Gray
$lblLast.Text = ""
$form.Controls.Add($lblLast)

$btnStart = New-Object System.Windows.Forms.Button
$btnStart.Text = "启动监听"
$btnStart.Location = New-Object System.Drawing.Point(18, 78)
$btnStart.Size = New-Object System.Drawing.Size(104, 34)
$btnStart.BackColor = [System.Drawing.Color]::FromArgb(232, 245, 233)
$form.Controls.Add($btnStart)

$btnStop = New-Object System.Windows.Forms.Button
$btnStop.Text = "停止监听"
$btnStop.Location = New-Object System.Drawing.Point(128, 78)
$btnStop.Size = New-Object System.Drawing.Size(104, 34)
$btnStop.BackColor = [System.Drawing.Color]::FromArgb(255, 235, 238)
$form.Controls.Add($btnStop)

$btnPause = New-Object System.Windows.Forms.Button
$btnPause.Text = "暂停"
$btnPause.Location = New-Object System.Drawing.Point(238, 78)
$btnPause.Size = New-Object System.Drawing.Size(96, 34)
$form.Controls.Add($btnPause)

$btnTest = New-Object System.Windows.Forms.Button
$btnTest.Text = "发测试消息"
$btnTest.Location = New-Object System.Drawing.Point(340, 78)
$btnTest.Size = New-Object System.Drawing.Size(112, 34)
$form.Controls.Add($btnTest)

$chkAuto = New-Object System.Windows.Forms.CheckBox
$chkAuto.Text = "开机自动启动"
$chkAuto.Location = New-Object System.Drawing.Point(20, 124)
$chkAuto.Size = New-Object System.Drawing.Size(160, 24)
$chkAuto.Checked = Test-Path $startupVbs
$form.Controls.Add($chkAuto)

$btnLog = New-Object System.Windows.Forms.Button
$btnLog.Text = "查看日志"
$btnLog.Location = New-Object System.Drawing.Point(200, 120)
$btnLog.Size = New-Object System.Drawing.Size(100, 30)
$form.Controls.Add($btnLog)

$btnFolder = New-Object System.Windows.Forms.Button
$btnFolder.Text = "打开目录"
$btnFolder.Location = New-Object System.Drawing.Point(306, 120)
$btnFolder.Size = New-Object System.Drawing.Size(100, 30)
$form.Controls.Add($btnFolder)

$txtLog = New-Object System.Windows.Forms.TextBox
$txtLog.Location = New-Object System.Drawing.Point(20, 162)
$txtLog.Size = New-Object System.Drawing.Size(432, 150)
$txtLog.Multiline = $true
$txtLog.ReadOnly = $true
$txtLog.ScrollBars = "Vertical"
$txtLog.Font = New-Object System.Drawing.Font("Consolas", 8.5)
$txtLog.BackColor = [System.Drawing.Color]::FromArgb(250, 250, 250)
$form.Controls.Add($txtLog)

$lblHint = New-Object System.Windows.Forms.Label
$lblHint.Location = New-Object System.Drawing.Point(20, 322)
$lblHint.Size = New-Object System.Drawing.Size(432, 20)
$lblHint.ForeColor = [System.Drawing.Color]::Gray
$lblHint.Text = "关闭这个窗口不会停止监听；监听本身在后台常驻。"
$form.Controls.Add($lblHint)

function Refresh-UI {
    $state = Get-BridgeState
    switch ($state) {
        "running" {
            $lblState.Text = "● 运行中"
            $lblState.ForeColor = [System.Drawing.Color]::FromArgb(46, 125, 50)
            $btnPause.Text = "暂停"
            $btnPause.Enabled = $true
        }
        "paused" {
            $lblState.Text = "● 已暂停（不处理新消息）"
            $lblState.ForeColor = [System.Drawing.Color]::FromArgb(239, 108, 0)
            $btnPause.Text = "恢复"
            $btnPause.Enabled = $true
        }
        default {
            $lblState.Text = "● 已停止"
            $lblState.ForeColor = [System.Drawing.Color]::FromArgb(117, 117, 117)
            $btnPause.Text = "暂停"
            $btnPause.Enabled = $false
        }
    }
    $btnStart.Enabled = ($state -eq "stopped")
    $btnStop.Enabled = ($state -ne "stopped")
    $chkAuto.Checked = Test-Path $startupVbs

    if (Test-Path $logPath) {
        try {
            $tail = Get-Content $logPath -Tail 6 -Encoding UTF8 -ErrorAction SilentlyContinue
            if ($null -ne $tail) { $txtLog.Text = ($tail -join "`r`n") }
        } catch { }
    }
}

$btnStart.Add_Click({
    Remove-Item $stopPath -Force -ErrorAction SilentlyContinue
    Start-Process -FilePath $pyw -ArgumentList @("`"$scriptPath`"") -WorkingDirectory $root -WindowStyle Hidden
    Start-Sleep -Milliseconds 1500
    Refresh-UI
})

$btnStop.Add_Click({
    if (Test-Path $lockPath) {
        $procId = [int](Get-Content $lockPath)
        Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
        Remove-Item $lockPath -Force -ErrorAction SilentlyContinue
    } else {
        Get-Process pythonw -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
    }
    Remove-Item $pausePath -Force -ErrorAction SilentlyContinue
    Remove-Item $stopPath -Force -ErrorAction SilentlyContinue
    Refresh-UI
})

$btnPause.Add_Click({
    if (Test-Path $pausePath) {
        Remove-Item $pausePath -Force
    } else {
        New-Item -ItemType File -Path $pausePath -Force | Out-Null
    }
    Refresh-UI
})

$script:testProc = $null
$btnTest.Add_Click({
    if ($null -ne $script:testProc -and -not $script:testProc.HasExited) { return }
    $text = [Microsoft.VisualBasic.Interaction]::InputBox("输入一条测试消息（会从「方正」发给「鳞羽」）：", "发测试消息", "在吗")
    if ([string]::IsNullOrWhiteSpace($text)) { return }
    $text = $text -replace '"', ''
    $argLine = '"' + $scriptPath + '" --test "' + $text + '"'
    $script:testProc = Start-Process -FilePath $py -ArgumentList $argLine -WorkingDirectory $root -WindowStyle Hidden -PassThru
    $lblLast.Text = "发送中…（约 10–20 秒）"
    $btnTest.Enabled = $false
})

$btnLog.Add_Click({
    if (Test-Path $logPath) { Start-Process notepad.exe $logPath } else { [System.Windows.Forms.MessageBox]::Show("还没有日志。") }
})

$btnFolder.Add_Click({ Start-Process explorer.exe $root })

$chkAuto.Add_Click({
    if ($chkAuto.Checked) {
        $vbs = 'Set sh = CreateObject("WScript.Shell")' + "`r`n" +
               'sh.Run """' + $pyw + '"" ""' + $scriptPath + '""", 0, False' + "`r`n"
        [System.IO.File]::WriteAllText($startupVbs, $vbs, [System.Text.Encoding]::ASCII)
    } else {
        Remove-Item $startupVbs -Force -ErrorAction SilentlyContinue
    }
})

$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = 1500
$timer.Add_Tick({
    Refresh-UI
    if ($null -ne $script:testProc -and $script:testProc.HasExited) {
        $script:testProc = $null
        $btnTest.Enabled = $true
        $last = ""
        if (Test-Path $logPath) {
            $line = Get-Content $logPath -Tail 5 -Encoding UTF8 | Where-Object { $_ -match "发送" } | Select-Object -Last 1
            if ($line) { $last = $line }
        }
        $lblLast.Text = $last
    }
})
$timer.Start()

Refresh-UI
[void]$form.ShowDialog()
$timer.Stop()
