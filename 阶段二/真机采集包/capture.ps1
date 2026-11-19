# 从 ESP32-CAM 抓帧，按"姿态标签"存到 captures\ 下，用于阈值标定
#
# 用法：
#   powershell -ExecutionPolicy Bypass -File .\capture.ps1 -Ip 192.168.1.23 -Label upright
#   powershell -ExecutionPolicy Bypass -File .\capture.ps1 -Ip 192.168.1.23 -Label slouch -Count 60 -IntervalMs 400
#
# 标签取值（与线上状态集一致）：
#   upright  坐直
#   slouch   低头/驼背
#   lean     侧歪
#   phone    玩手机
#   sleep    趴桌
#   away     离开座位（空桌）
#   unknown  故障帧（遮挡/过暗/镜头朝天）—— 很有用，务必采一组
param(
    [Parameter(Mandatory=$true)][string]$Ip,
    [Parameter(Mandatory=$true)][string]$Label,
    [int]$Count = 40,
    [int]$IntervalMs = 500,
    [int]$TimeoutMs = 8000,
    [string]$OutRoot = ""
)

$ErrorActionPreference = 'Stop'
# 脚本目录兜底：某些调用方式下 $PSScriptRoot 为空
$ScriptDir = if ($PSScriptRoot) { $PSScriptRoot } else { Split-Path -Parent $MyInvocation.MyCommand.Path }
if (-not $OutRoot) { $OutRoot = Join-Path $ScriptDir 'captures' }
$valid = 'upright','slouch','lean','phone','sleep','away','unknown'
if ($valid -notcontains $Label) {
    Write-Host "标签必须是: $($valid -join ' / ')" -ForegroundColor Red; exit 1
}

$base = "http://$Ip"
Write-Host "=== 1. 检查设备 ===" -ForegroundColor Cyan
try {
    $st = Invoke-RestMethod -Uri "$base/status" -TimeoutSec 5
    Write-Host ("设备在线: cam_ok={0} ip={1} rssi={2} 已服务帧={3}" -f $st.cam_ok, $st.ip, $st.rssi, $st.frames) -ForegroundColor Green
    if (-not $st.cam_ok) { Write-Host "摄像头不可用，检查排线" -ForegroundColor Red; exit 1 }
} catch {
    Write-Host "连不上 $base —— 确认 IP 正确、和电脑在同一网络" -ForegroundColor Red; exit 1
}

$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$dir = Join-Path $OutRoot ("{0}-{1}" -f $stamp, $Label)
New-Item -ItemType Directory -Force -Path $dir | Out-Null

Write-Host "=== 2. 开始采集 $Count 张，间隔 ${IntervalMs}ms ===" -ForegroundColor Cyan
Write-Host "    输出: $dir" -ForegroundColor DarkGray
$ok = 0; $fail = 0; $sizes = @()
for ($i = 1; $i -le $Count; $i++) {
    $path = Join-Path $dir ("frame-{0:D4}.jpg" -f $i)
    try {
        Invoke-WebRequest -Uri "$base/capture?ts=$i" -OutFile $path -TimeoutSec ($TimeoutMs/1000) -UseBasicParsing | Out-Null
        $len = (Get-Item $path).Length
        if ($len -lt 3000) { throw "帧太小($len B)" }
        $sizes += $len; $ok++
    } catch {
        $fail++
        Write-Host ("  #{0} 失败: {1}" -f $i, $_.Exception.Message) -ForegroundColor Yellow
    }
    if ($i % 10 -eq 0) { Write-Host ("  {0}/{1}" -f $i, $Count) -ForegroundColor DarkGray }
    Start-Sleep -Milliseconds $IntervalMs
}

$meta = [ordered]@{
    label     = $Label
    device_ip = $Ip
    count     = $ok
    failed    = $fail
    interval_ms = $IntervalMs
    started   = $stamp
    avg_bytes = if ($sizes.Count) { [int](($sizes | Measure-Object -Average).Average) } else { 0 }
    note      = "ESP32-CAM VGA / q12 / OV3660 vflip"
}
$meta | ConvertTo-Json | Set-Content -Path (Join-Path $dir 'meta.json') -Encoding UTF8

Write-Host ""
Write-Host "=== 完成 ===" -ForegroundColor Green
Write-Host ("成功 {0} 张，失败 {1} 张，平均 {2:N0} 字节" -f $ok, $fail, $meta.avg_bytes)
Write-Host "把这个目录整个发回来即可（$dir）" -ForegroundColor Yellow
Write-Host ""
Write-Host "建议采集组合（同一机位不要动相机）：" -ForegroundColor Cyan
Write-Host "  upright 40 / slouch 40 / phone 30 / sleep 20 / away 10 / unknown 10"
