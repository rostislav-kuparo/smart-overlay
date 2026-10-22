<#
  Установка Smart Overlay: виртуальное окружение, зависимости и ярлык в меню «Пуск»
  (после этого программа находится через поиск Windows).

  install.bat              — установить или обновить
  install.bat -Desktop     — ещё и ярлык на рабочем столе
  install.bat -Uninstall   — убрать ярлыки (настройки в %APPDATA%\SmartOverlay остаются)
#>
param([switch]$Desktop, [switch]$Uninstall)
$ErrorActionPreference = 'Stop'

$root = $PSScriptRoot
$startMenuLink = Join-Path ([Environment]::GetFolderPath('Programs')) 'Smart Overlay.lnk'
$desktopLink = Join-Path ([Environment]::GetFolderPath('Desktop')) 'Smart Overlay.lnk'

if ($Uninstall) {
    Remove-Item $startMenuLink, $desktopLink -ErrorAction SilentlyContinue
    Write-Host "Ярлыки удалены. Настройки остались в $env:APPDATA\SmartOverlay"
    return
}

$python = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) {
    Write-Host 'Создаю виртуальное окружение...'
    if (Get-Command py -ErrorAction SilentlyContinue) { py -3 -m venv (Join-Path $root '.venv') }
    else { python -m venv (Join-Path $root '.venv') }
    if (-not (Test-Path $python)) { throw 'Не удалось создать .venv. Установите Python 3.11+ с python.org' }
}

Write-Host 'Устанавливаю зависимости (в первый раз около 2 ГБ)...'
& $python -m pip install --upgrade pip --quiet
& $python -m pip install --editable $root
if ($LASTEXITCODE) { throw 'pip install завершился с ошибкой' }

$targets = @($startMenuLink)
if ($Desktop) { $targets += $desktopLink }
$shell = New-Object -ComObject WScript.Shell
foreach ($path in $targets) {
    $link = $shell.CreateShortcut($path)
    $link.TargetPath = Join-Path $root '.venv\Scripts\smart-overlay.exe'
    $link.WorkingDirectory = $root
    $link.IconLocation = Join-Path $root 'smart_overlay\assets\icon.ico'
    $link.Description = 'Ответы Gemini на вопросы из звука ПК поверх экрана'
    $link.Save()
    Write-Host "Ярлык: $path"
}
Write-Host ''
Write-Host 'Готово. Наберите «Smart Overlay» в поиске Windows или найдите программу в меню «Пуск».'
