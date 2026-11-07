$root = Split-Path -Parent $PSScriptRoot
$env:Path = [Environment]::GetEnvironmentVariable('Path','Machine') + ';' + [Environment]::GetEnvironmentVariable('Path','User') + ';' + $env:Path
$python = Join-Path $PSScriptRoot '.venv\Scripts\pythonw.exe'
$app = Join-Path $root 'app.py'
if (-not (Test-Path -LiteralPath $python) -or -not (Test-Path -LiteralPath $app)) { Write-Host 'Application or Python is missing.'; exit 1 }
Set-Location -LiteralPath $root
& $python $app
exit $LASTEXITCODE
