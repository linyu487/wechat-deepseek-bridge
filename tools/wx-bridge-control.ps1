param([switch]$Stop)

$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$py = "CHANGE_ME_PYTHON_DIR"
$lock = Join-Path $root "tmp\bridge.lock"

if ($Stop) {
    if (Test-Path $lock) {
        $procId = [int](Get-Content $lock)
        Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
        Remove-Item $lock -Force -ErrorAction SilentlyContinue
        Write-Host "stopped pid=$procId"
    } else {
        Get-Process pythonw -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
        Write-Host "stopped (no lock file)"
    }
    exit 0
}

Start-Process -FilePath (Join-Path $py "pythonw.exe") `
    -ArgumentList @((Join-Path $root "tools\wx-bridge.py")) `
    -WorkingDirectory $root -WindowStyle Hidden
Start-Sleep -Seconds 2
if (Test-Path $lock) { Write-Host "started pid=$(Get-Content $lock)" } else { Write-Host "may have failed, check tmp\bridge.log" }
