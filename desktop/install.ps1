$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
$root = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $root
$ProgressPreference = 'SilentlyContinue'

function Refresh-Path {
    $machine = [Environment]::GetEnvironmentVariable('Path', 'Machine')
    $user = [Environment]::GetEnvironmentVariable('Path', 'User')
    $env:Path = "$machine;$user;$env:Path"
}
function Install-Winget([string]$id) {
    if (-not (Get-Command winget.exe -ErrorAction SilentlyContinue)) {
        throw 'Не найден winget. Установите App Installer из Microsoft Store и повторите запуск.'
    }
    Write-Host "Установка $id через winget..."
    & winget.exe install --id $id --exact --source winget --accept-source-agreements --accept-package-agreements --disable-interactivity
    if ($LASTEXITCODE -ne 0) { throw "Не удалось установить $id (код $LASTEXITCODE)." }
    Refresh-Path
}
function Run-Checked([string]$file, [string[]]$arguments) {
    & $file @arguments
    if ($LASTEXITCODE -ne 0) { throw "Ошибка команды $file (код $LASTEXITCODE)." }
}
function Python-312 {
    $py = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($py) {
        $candidate = & $py.Source -3.12 -c 'import sys;print(sys.executable)' 2>$null
        if ($LASTEXITCODE -eq 0 -and $candidate -and (Test-Path -LiteralPath $candidate)) { return $candidate }
    }
    $candidate = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe'
    if (Test-Path -LiteralPath $candidate) { return $candidate }
    return $null
}
function Ollama-Exe {
    $local = Join-Path $env:LOCALAPPDATA 'Programs\Ollama\ollama.exe'
    if (Test-Path -LiteralPath $local) { return $local }
    $exe = Get-Command ollama.exe -ErrorAction SilentlyContinue
    if ($exe) { return $exe.Source }
    return $null
}
function Api-Ready {
    try {
        $response = Invoke-WebRequest -Uri 'http://127.0.0.1:11434/api/tags' -UseBasicParsing -TimeoutSec 3
        return $response.StatusCode -eq 200
    } catch { return $false }
}
try {
    Write-Host 'Установка зависимостей Windows. Существующий config.json и личные данные не изменяются.'
    Write-Host 'Это подготовка, а не готовое приложение; интерфейс ещё не подключён.' -ForegroundColor Yellow
    Refresh-Path
    $python = Python-312
    if (-not $python) { Install-Winget 'Python.Python.3.12'; $python = Python-312 }
    if (-not $python) { throw 'Python 3.12 установлен, но не найден. Перезапустите установщик.' }

    if (-not (Get-Command ffmpeg.exe -ErrorAction SilentlyContinue) -or -not (Get-Command ffprobe.exe -ErrorAction SilentlyContinue)) {
        Install-Winget 'Gyan.FFmpeg'
    }
    if (-not (Get-Command ffmpeg.exe -ErrorAction SilentlyContinue) -or -not (Get-Command ffprobe.exe -ErrorAction SilentlyContinue)) {
        throw 'FFmpeg установлен, но ffmpeg/ffprobe не найдены в PATH. Перезапустите установщик.'
    }

    $ollama = Ollama-Exe
    if (-not $ollama) { Install-Winget 'Ollama.Ollama'; $ollama = Ollama-Exe }
    if (-not $ollama) { throw 'Ollama установлен, но ollama.exe не найден. Перезапустите установщик.' }

    $venv = Join-Path $PSScriptRoot '.venv'
    $venvPython = Join-Path $venv 'Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $venvPython)) { Run-Checked -file $python -arguments @('-m','venv',$venv) }
    Run-Checked -file $venvPython -arguments @('-m','pip','install','--upgrade','pip')
    Run-Checked -file $venvPython -arguments @('-m','pip','install','-r',(Join-Path $PSScriptRoot 'requirements-desktop.txt'))

    # The pinned original GitHub release is verified before unpacking. Never replace an existing tool.
    $tools = Join-Path $root 'desktop\tools\TwitchDownloaderCLI'
    $cli = Join-Path $tools 'TwitchDownloaderCLI.exe'
    if (-not (Test-Path -LiteralPath $cli)) {
        if (Test-Path -LiteralPath $tools) { throw "Папка $tools уже существует, но CLI в ней не найден; она не будет перезаписана." }
        $temp = Join-Path ([System.IO.Path]::GetTempPath()) ('narezka-' + [Guid]::NewGuid().ToString('N'))
        New-Item -ItemType Directory -Path $temp | Out-Null
        try {
            $zip = Join-Path $temp 'twitch.zip'
            $url = 'https://github.com/lay295/TwitchDownloader/releases/download/1.56.5/TwitchDownloaderCLI-1.56.5-Windows-x64.zip'
            Write-Host 'Скачивание TwitchDownloaderCLI...'
            Invoke-WebRequest -Uri $url -OutFile $zip -UseBasicParsing
            $hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $zip).Hash.ToLowerInvariant()
            if ($hash -ne '8b1b0695f2b1b6bf0d2535fab4b84032951cded8cf4078dfdf4d58e391c813a0') { throw 'Контрольная сумма TwitchDownloaderCLI не совпадает.' }
            $extract = Join-Path $temp 'extracted'
            Expand-Archive -LiteralPath $zip -DestinationPath $extract
            $exe = Get-ChildItem -LiteralPath $extract -Filter 'TwitchDownloaderCLI.exe' -File -Recurse | Select-Object -First 1
            if (-not $exe) { throw 'TwitchDownloaderCLI.exe отсутствует в архиве.' }
            New-Item -ItemType Directory -Force -Path (Split-Path -Parent $tools) | Out-Null
            Move-Item -LiteralPath $exe.Directory.FullName -Destination $tools
        } finally { Remove-Item -LiteralPath $temp -Recurse -Force -ErrorAction SilentlyContinue }
    }

    if (-not (Api-Ready)) {
        Start-Process -FilePath $ollama -ArgumentList 'serve' -WindowStyle Hidden | Out-Null
        for ($i=0; $i -lt 30 -and -not (Api-Ready); $i++) { Start-Sleep -Seconds 1 }
    }
    if (-not (Api-Ready)) { throw 'Не удалось запустить локальный сервер Ollama на 127.0.0.1:11434.' }
    Write-Host 'Загрузка локальной модели Qwen3-8B (~5,2 ГБ)...'
    Run-Checked -file $ollama -arguments @('pull','qwen3:8b')
    Write-Host 'Загрузка модели Whisper large-v3-turbo...'
    Run-Checked -file $venvPython -arguments @('-c','from faster_whisper.utils import download_model; download_model("large-v3-turbo")')
    Write-Host 'Зависимости и модели подготовлены. Приложение и автоматический конвейер ещё не готовы.' -ForegroundColor Yellow
    exit 0
} catch {
    Write-Host ("ОШИБКА: " + $_.Exception.Message) -ForegroundColor Red
    exit 1
}
