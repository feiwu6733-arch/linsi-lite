﻿param([int]$Port = 5030, [switch]$NoBrowser, [string]$DataDir = '')
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$runtime = Get-Command python -ErrorAction SilentlyContinue
if (-not $runtime) { $runtime = Get-Command py -ErrorAction SilentlyContinue }
if (-not $runtime) { throw 'Python 3.11+ is required. Install Python, then run this script again.' }
$venvPython = Join-Path $PSScriptRoot '.venv/Scripts/python.exe'
$pythonPath = if (Test-Path -LiteralPath $venvPython) { $venvPython } else { $runtime.Source }
$existing = $null
try { $existing = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/health" -TimeoutSec 2 } catch {}
if ($existing -and $existing.product -eq 'linsi-lite') {
    $versionLine = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'linsi/__init__.py') | Select-String 'VERSION = "([0-9.]+)"'
    $diskVersion = [version]$versionLine.Matches[0].Groups[1].Value
    if ([version]$existing.version -lt $diskVersion) { throw 'The old Linsi version is still running. Close its launch window, then start this version again.' }
    if (-not $NoBrowser) { & $pythonPath -m webbrowser "http://127.0.0.1:$Port" }
    Write-Host "Linsi is already running at http://127.0.0.1:$Port"
    exit 0
}
$launchArgs = @('app.py', '--port', "$Port")
if ($DataDir) { $launchArgs += @('--data-dir', $DataDir) }
if ($NoBrowser) { $launchArgs += '--no-browser' }
& $pythonPath @launchArgs
exit $LASTEXITCODE
