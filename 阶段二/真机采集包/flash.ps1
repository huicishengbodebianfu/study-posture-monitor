# 烧录采集固件到 ESP32-CAM（arduino-cli）
#
# 用法：
#   powershell -ExecutionPolicy Bypass -File .\flash.ps1
#   powershell -ExecutionPolicy Bypass -File .\flash.ps1 -Com COM5
#
# 接线（AI Thinker ESP32-CAM + USB-TTL）：
#   GPIO0 接地  →  按一下 RST  →  再烧录
#   烧完把 GPIO0 断开，再按 RST 进入运行模式
param(
    [string]$Com = "",
    [string]$Sketch = ""
)

$ErrorActionPreference = 'Stop'
$ScriptDir = if ($PSScriptRoot) { $PSScriptRoot } else { Split-Path -Parent $MyInvocation.MyCommand.Path }
if (-not $Sketch) { $Sketch = Join-Path $ScriptDir 'firmware\capture' }
$fqbn = 'esp32:esp32:esp32cam'

# arduino-cli：优先用工作区里的，其次是 PATH
$cli = Join-Path $ScriptDir "..\..\__tools\arduino-cli\arduino-cli.exe"
if (-not (Test-Path $cli)) { $cli = (Get-Command arduino-cli -ErrorAction SilentlyContinue).Source }
if (-not $cli -or -not (Test-Path $cli)) {
    Write-Host "找不到 arduino-cli.exe" -ForegroundColor Red
    Write-Host "把 arduino-cli 放到 __tools\arduino-cli\ 下，或加进 PATH" -ForegroundColor Yellow
    exit 1
}
Write-Host "arduino-cli: $cli" -ForegroundColor DarkGray

# 检查 esp32 core
Write-Host "=== 0. 检查 esp32 core ===" -ForegroundColor Cyan
$cores = & $cli core list 2>$null
if ($cores -notmatch 'esp32:esp32') {
    Write-Host "未安装 esp32 core，正在安装（首次约 3~5 分钟）..." -ForegroundColor Yellow
    & $cli core update-index
    & $cli core install esp32:esp32
    if ($LASTEXITCODE -ne 0) { Write-Host "core 安装失败，检查网络" -ForegroundColor Red; exit 1 }
}

# 选串口
if (-not $Com) {
    Write-Host "=== 1. 查找串口 ===" -ForegroundColor Cyan
    $list = & $cli board list
    $list
    $m = ($list | Select-String -Pattern '(COM\d+)' | Select-Object -First 1)
    if ($m) { $Com = $m.Matches[0].Groups[1].Value }
    else { $Com = Read-Host "未识别到串口，请手动输入（如 COM5）" }
}
Write-Host "使用串口: $Com" -ForegroundColor Green

# 编译
Write-Host "=== 2. 编译 ===" -ForegroundColor Cyan
& $cli compile --fqbn $fqbn $Sketch
if ($LASTEXITCODE -ne 0) { Write-Host "编译失败" -ForegroundColor Red; exit 1 }

# 上传
Write-Host "=== 3. 上传（确认 GPIO0 已接地并按过 RST）===" -ForegroundColor Cyan
& $cli upload -p $Com --fqbn $fqbn $Sketch
if ($LASTEXITCODE -ne 0) {
    Write-Host "上传失败：多为 GPIO0 没接地 / 没按 RST / 串口被占用" -ForegroundColor Red
    exit 1
}

Write-Host ""
Write-Host "=== 完成 ===" -ForegroundColor Green
Write-Host "1) 断开 GPIO0 与 GND，按一下 RST"
Write-Host "2) 运行下面命令看 IP（Ctrl+C 退出）："
Write-Host "   `"$cli`" monitor -p $Com --config baudrate=115200" -ForegroundColor Yellow
Write-Host "3) 记下 IP，运行 .\capture.ps1 -Ip <IP> -Label upright"
