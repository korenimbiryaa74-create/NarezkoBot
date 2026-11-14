$root = Split-Path -Parent $PSScriptRoot
$env:Path = [Environment]::GetEnvironmentVariable('Path','Machine') + ';' + [Environment]::GetEnvironmentVariable('Path','User') + ';' + $env:Path
$python = Join-Path $PSScriptRoot '.venv\Scripts\pythonw.exe'
$console = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
$requirements = Join-Path $PSScriptRoot 'requirements-desktop.txt'
# New Flet interface; the old Tk window (app.py) stays as a fallback.
$app = Join-Path $root 'app_flet.py'
$classic = Join-Path $root 'app.py'
if (-not (Test-Path -LiteralPath $python) -or -not (Test-Path -LiteralPath $classic)) { Write-Host 'Application or Python is missing.'; exit 1 }
Set-Location -LiteralPath $root
& $console -c 'import cv2, flet' 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host 'Installing desktop dependencies (OpenCV, Flet)...'
    & $console -m pip install --disable-pip-version-check -r $requirements
}
& $console -c 'import flet' 2>$null
if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $app) -or $env:NAREZKO_CLASSIC_UI -eq '1') {
    Write-Host 'Starting the classic interface.'
    $app = $classic
}
& $python $app
exit $LASTEXITCODE
