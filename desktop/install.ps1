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
function Test-Python315([string]$exe, [string[]]$prefix = @()) {
    if (-not $exe) { return $null }
    $oldPreference = $ErrorActionPreference
    $oldAutoInstall = $env:PYTHON_MANAGER_AUTOMATIC_INSTALL
    $oldEncoding = $env:PYTHONIOENCODING
    try {
        # Never let Python install manager download a runtime while searching.
        $env:PYTHON_MANAGER_AUTOMATIC_INSTALL = '0'
        $env:PYTHONIOENCODING = 'utf-8'
        $ErrorActionPreference = 'Continue'
        $probe = Join-Path $PSScriptRoot 'python_probe.py'
        $params = @($prefix) + @($probe)
        $candidate = & $exe @params 2>$null
        if ($LASTEXITCODE -eq 0 -and $candidate -is [string] -and (Test-Path -LiteralPath $candidate -PathType Leaf)) {
            return $candidate
        }
    } catch {
        # Missing/stub executable or an unusable runtime: try the next candidate.
    } finally {
        $ErrorActionPreference = $oldPreference
        $env:PYTHON_MANAGER_AUTOMATIC_INSTALL = $oldAutoInstall
        $env:PYTHONIOENCODING = $oldEncoding
    }
    return $null
}
function Python-315 {
    $py = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($py) {
        $candidate = Test-Python315 -exe $py.Source -prefix @('-3.15')
        if ($candidate) { return $candidate }
    }
    foreach ($name in @('python3.15.exe','python.exe')) {
        $command = Get-Command $name -ErrorAction SilentlyContinue
        if ($command) {
            $candidate = Test-Python315 -exe $command.Source
            if ($candidate) { return $candidate }
        }
    }
    # PEP 514 installations may live outside the standard user folder.
    $registryRoots = @('HKCU:\Software\Python','HKLM:\Software\Python','HKLM:\Software\WOW6432Node\Python')
    foreach ($registryRoot in $registryRoots) {
        if (-not (Test-Path $registryRoot)) { continue }
        $vendors = Get-ChildItem -Path $registryRoot -ErrorAction SilentlyContinue
        foreach ($vendor in $vendors) {
            foreach ($version in (Get-ChildItem -Path $vendor.PSPath -ErrorAction SilentlyContinue)) {
                $installKey = Join-Path $version.PSPath 'InstallPath'
                if (-not (Test-Path $installKey)) { continue }
                $key = Get-Item -Path $installKey -ErrorAction SilentlyContinue
                $path = $key.GetValue('ExecutablePath')
                if (-not $path) {
                    $dir = $key.GetValue('')
                    if ($dir) { $path = Join-Path $dir 'python.exe' }
                }
                if ($path) {
                    $candidate = Test-Python315 -exe $path
                    if ($candidate) { return $candidate }
                }
            }
        }
    }
    $paths = @()
    if ($env:LOCALAPPDATA) { $paths += Join-Path $env:LOCALAPPDATA 'Programs\Python\Python315\python.exe' }
    if ($env:ProgramFiles) { $paths += Join-Path $env:ProgramFiles 'Python315\python.exe' }
    $paths += 'C:\Python315\python.exe'
    foreach ($path in $paths) {
        if (Test-Path -LiteralPath $path -PathType Leaf) {
            $candidate = Test-Python315 -exe $path
            if ($candidate) { return $candidate }
        }
    }
    Write-Host 'Автоматически найти Python 3.15 x64 не удалось.' -ForegroundColor Yellow
    Write-Host 'Введите полный путь к python.exe или перетащите этот файл в окно. Enter отменяет установку.'
    $entered = (Read-Host 'Путь к python.exe').Trim().Trim('"')
    if ($entered -and (Test-Path -LiteralPath $entered -PathType Leaf)) {
        $candidate = Test-Python315 -exe $entered
        if ($candidate) { return $candidate }
        Write-Host 'Файл не запустился или это не 64-битный Python 3.15.' -ForegroundColor Yellow
    }
    return $null
}
function Download-Verified([string]$url, [string]$path, [string]$sha256) {
    if (Test-Path -LiteralPath $path) {
        if ((Get-FileHash -Algorithm SHA256 -LiteralPath $path).Hash.ToLowerInvariant() -ne $sha256) {
            throw "Существующий файл $path повреждён или изменён; он не будет перезаписан."
        }
        return
    }
    $tmp = "$path.download-" + [Guid]::NewGuid().ToString('N')
    try {
        Run-Checked -file 'curl.exe' -arguments @('--fail','--location','--retry','3','--output',$tmp,$url)
        if ((Get-FileHash -Algorithm SHA256 -LiteralPath $tmp).Hash.ToLowerInvariant() -ne $sha256) { throw "Контрольная сумма $url не совпадает." }
        Move-Item -LiteralPath $tmp -Destination $path
    } finally { Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue }
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
    Write-Host 'Экспериментальная сборка: интерфейс есть, но приложение ещё не проверено на Windows.' -ForegroundColor Yellow
    Refresh-Path
    $python = Python-315
    if (-not $python) { throw 'Python 3.15 x64 не найден. Ничего не устанавливалось и текущий Python не изменялся.' }
    Write-Host "Python: $python"

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
    if (-not (Test-Python315 -exe $venvPython)) { throw 'Существующее desktop/.venv не использует рабочий 64-битный Python 3.15; оно не изменено.' }
    Run-Checked -file $venvPython -arguments @('-m','pip','install','--upgrade','pip')
    Run-Checked -file $venvPython -arguments @('-m','pip','install','-r',(Join-Path $PSScriptRoot 'requirements-desktop.txt'))

    Run-Checked -file $venvPython -arguments @('-c','import tkinter,requests,numpy,websocket,cv2')

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
    $whisper = Join-Path $root 'desktop\tools\whisper'
    $whisperCli = Join-Path $whisper 'whisper-cli.exe'
    if (-not (Test-Path -LiteralPath $whisperCli)) {
        if (Test-Path -LiteralPath $whisper) { throw "Папка $whisper уже существует, но whisper-cli.exe не найден; она не будет перезаписана." }
        $temp = Join-Path ([System.IO.Path]::GetTempPath()) ('narezka-whisper-' + [Guid]::NewGuid().ToString('N'))
        New-Item -ItemType Directory -Path $temp | Out-Null
        try {
            $zip = Join-Path $temp 'whisper.zip'
            Write-Host 'Скачивание whisper.cpp CUDA (~653 МиБ)...'
            Download-Verified 'https://github.com/ggml-org/whisper.cpp/releases/download/b5454/whisper-bin-win-cuda-12.4.0-x64.zip' $zip 'afef0b881c500958921c3f5523b50e59ee2ec9b6f5cbd25b324c51ed308a957a'
            $extract = Join-Path $temp 'extracted'
            Expand-Archive -LiteralPath $zip -DestinationPath $extract
            $exe = Get-ChildItem -LiteralPath $extract -Filter 'whisper-cli.exe' -File -Recurse | Select-Object -First 1
            if (-not $exe) { throw 'whisper-cli.exe отсутствует в архиве.' }
            New-Item -ItemType Directory -Force -Path (Split-Path -Parent $whisper) | Out-Null
            Move-Item -LiteralPath $exe.Directory.FullName -Destination $whisper
        } finally { Remove-Item -LiteralPath $temp -Recurse -Force -ErrorAction SilentlyContinue }
    }
    $modelDir = Join-Path $root 'desktop\tools\whisper-model'
    New-Item -ItemType Directory -Force -Path $modelDir | Out-Null
    $model = Join-Path $modelDir 'ggml-large-v3-turbo.bin'
    Write-Host 'Скачивание модели Whisper large-v3-turbo (~1,62 ГБ)...'
    Download-Verified 'https://huggingface.co/ggerganov/whisper.cpp/resolve/98aa99a0a9db05ae2342309f5096248665f7cba3/ggml-large-v3-turbo.bin' $model '1fc70f774d38eb169993ac391eea357ef47c88757ef72ee5943879b7e8e2bc69'
    Run-Checked -file $whisperCli -arguments @('--version')
    . (Join-Path $PSScriptRoot 'install_preview.ps1')
    Install-LocalPreview
    Write-Host 'Зависимости подготовлены. Сборка экспериментальная; Windows и GPU пока не проверены.' -ForegroundColor Yellow
    exit 0
} catch {
    Write-Host ("ОШИБКА: " + $_.Exception.Message) -ForegroundColor Red
    exit 1
}
