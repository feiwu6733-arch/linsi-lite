param([ValidateSet('browser','transcription')][string]$Component = 'browser')
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot
$venvPython = Join-Path $projectRoot '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $venvPython)) {
    $runtime = Get-Command python -ErrorAction SilentlyContinue
    if (-not $runtime) { throw 'Please install Python 3.11 or newer first.' }
    & $runtime.Source -m venv (Join-Path $projectRoot '.venv')
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the isolated Python environment.' }
}
& $venvPython -m pip install -r (Join-Path $projectRoot 'requirements-browser.txt')
if ($LASTEXITCODE -ne 0) { throw 'Browser component installation failed. Please check your network.' }
if ($Component -eq 'transcription') {
    & $venvPython -m pip install -r (Join-Path $projectRoot 'requirements-transcription.txt')
    if ($LASTEXITCODE -ne 0) { throw 'Transcription component installation failed. Please check Python compatibility and network.' }
}
Write-Host 'Components installed in .venv. Close the old Linsi window, then run the launcher again.'
Write-Host 'Edge or Chrome is required. Model files are prepared separately from the task page.'
