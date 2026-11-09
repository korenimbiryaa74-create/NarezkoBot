# Called from install.ps1; shares Run-Checked, Download-Verified and Install-Winget.
# Keep the partial archive across connection failures and installer restarts.
function Download-PreviewArchive([string]$archive) {
    $url = 'https://github.com/Comfy-Org/ComfyUI/releases/download/v0.44.0/ComfyUI_windows_portable_nvidia.7z'
    $expected = '7170c2db49c182a81adcc051e80234f99dd891656fb200737f87797876348f96'
    $expectedSize = [Int64]2054382457
    $part = "$archive.part"
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $archive) | Out-Null
    if (Test-Path -LiteralPath $archive) {
        if ((Get-FileHash -Algorithm SHA256 -LiteralPath $archive).Hash.ToLowerInvariant() -ne $expected) { throw "Архив $archive отличается от официального и не изменён." }
        return
    }
    for ($attempt = 1; $attempt -le 5; $attempt++) {
        if (Test-Path -LiteralPath $part) {
            $bytes = (Get-Item -LiteralPath $part).Length
            if ($bytes -gt $expectedSize) { throw "Файл $part слишком большой и не изменён. Уберите его вручную и повторите установку." }
            if ($bytes -eq $expectedSize) {
                Write-Host 'Проверка контрольной суммы архива ComfyUI...'
                if ((Get-FileHash -Algorithm SHA256 -LiteralPath $part).Hash.ToLowerInvariant() -ne $expected) { throw "Контрольная сумма $part не совпадает. Файл сохранён; уберите его вручную и запустите установку снова." }
                Move-Item -LiteralPath $part -Destination $archive
                return
            }
            Write-Host "Продолжаю скачивание ComfyUI: сохранено $([Math]::Round($bytes / 1MB)) МиБ."
        }
        & curl.exe --fail --location --continue-at - --retry 5 --retry-all-errors --retry-delay 5 --connect-timeout 30 --speed-limit 1024 --speed-time 120 --output $part $url
        $curlCode = $LASTEXITCODE
        if ((Test-Path -LiteralPath $part) -and (Get-Item -LiteralPath $part).Length -eq $expectedSize) { continue }
        if ($attempt -lt 5) { Write-Host "Соединение прервано (код $curlCode); повторяю с сохранённого места..."; Start-Sleep -Seconds 5 }
    }
    if ((Test-Path -LiteralPath $part) -and (Get-Item -LiteralPath $part).Length -eq $expectedSize) {
        if ((Get-FileHash -Algorithm SHA256 -LiteralPath $part).Hash.ToLowerInvariant() -ne $expected) { throw "Контрольная сумма $part не совпадает. Файл сохранён; уберите его вручную и запустите установку снова." }
        Move-Item -LiteralPath $part -Destination $archive
        return
    }
    throw "Не удалось закончить загрузку ComfyUI. Частичный файл сохранён: $part. Проверьте интернет и повторите INSTALL.bat."
}

function Install-LocalPreview {
    Write-Host ''
    Write-Host 'Тест превью: локальный ComfyUI и FLUX Klein, загрузка около 20 ГБ; рекомендуется 35 ГБ свободного места.'
    Write-Host 'Нужен бесплатный аккаунт Hugging Face, принятие лицензии модели и токен с доступом к ней.'
    $answer = Read-Host 'Установить генератор превью сейчас? [y/N]'
    if ($answer -notmatch '^(y|yes|д|да)$') { Write-Host 'Генератор пропущен; установить его можно повторным запуском INSTALL.bat.'; return }
    Write-Host 'Откройте https://huggingface.co/black-forest-labs/flux-3-image-klein-comfy и примите условия модели.'
    Write-Host 'Создайте токен чтения: https://huggingface.co/settings/tokens (разрешите доступ к gated repositories).'
    Write-Host 'Здесь ключ НЕ вводите. Просто нажмите Enter; запрос ключа появится позже.'
    do { $pressedKey = [Console]::ReadKey($true) } while ($pressedKey.Key -ne [ConsoleKey]::Enter)
    Write-Host ''
    $comfyHome = Join-Path $PSScriptRoot 'tools\ComfyUI_windows_portable'
    $comfy = Join-Path $comfyHome 'ComfyUI\main.py'
    $py = Join-Path $comfyHome 'python_embeded\python.exe'
    if (-not (Test-Path -LiteralPath $comfy) -or -not (Test-Path -LiteralPath $py)) {
        if (Test-Path -LiteralPath $comfyHome) { throw "Папка $comfyHome уже существует, но неполная; она не изменена." }
        $seven = Join-Path $env:ProgramFiles '7-Zip\7z.exe'
        if (-not (Test-Path -LiteralPath $seven)) {
            $cmd = Get-Command 7z.exe -ErrorAction SilentlyContinue
            if ($cmd) { $seven = $cmd.Source } else { Install-Winget '7zip.7zip' }
        }
        if (-not (Test-Path -LiteralPath $seven)) { $cmd = Get-Command 7z.exe -ErrorAction SilentlyContinue; if ($cmd) {$seven = $cmd.Source} }
        if (-not (Test-Path -LiteralPath $seven)) { throw 'Не найден 7-Zip; перезапустите установщик.' }
        $temp = Join-Path ([System.IO.Path]::GetTempPath()) ('narezko-comfy-' + [Guid]::NewGuid().ToString('N'))
        New-Item -ItemType Directory -Path $temp | Out-Null
        try {
            $archive = Join-Path $PSScriptRoot 'tools\downloads\ComfyUI-v0.44.0-nvidia.7z'
            Write-Host 'Загрузка официального ComfyUI portable (~2 ГБ) с поддержкой продолжения...'
            Download-PreviewArchive $archive
            $extract = Join-Path $temp 'extract'
            Run-Checked -file $seven -arguments @('x','-y',$archive,('-o' + $extract))
            $unpacked = Join-Path $extract 'ComfyUI_windows_portable'
            if (-not (Test-Path -LiteralPath (Join-Path $unpacked 'ComfyUI\main.py')) -or -not (Test-Path -LiteralPath (Join-Path $unpacked 'python_embeded\python.exe'))) { throw 'Структура архива ComfyUI не соответствует ожидаемой.' }
            New-Item -ItemType Directory -Force -Path (Split-Path -Parent $comfyHome) | Out-Null
            Move-Item -LiteralPath $unpacked -Destination $comfyHome
        } finally { Remove-Item -LiteralPath $temp -Recurse -Force -ErrorAction SilentlyContinue }
    }
    Run-Checked -file $venvPython -arguments @('-m','pip','install','Pillow>=11')
    Run-Checked -file $py -arguments @('-m','pip','install','huggingface_hub>=0.34,<2')
    Write-Host 'Вставьте токен Hugging Face в приглашении ниже; он не добавляется в настройки Нарезкобота.'
    Run-Checked -file $py -arguments @('-c','from huggingface_hub import login; login(skip_if_logged_in=True)')
    Run-Checked -file $py -arguments @((Join-Path $PSScriptRoot 'preview_download_models.py'),(Join-Path $comfyHome 'ComfyUI\models'))
    Write-Host 'Локальный генератор превью установлен.' -ForegroundColor Green
}
