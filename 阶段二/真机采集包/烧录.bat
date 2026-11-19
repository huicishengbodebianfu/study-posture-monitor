@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
title ESP32 相机烧录（零安装）
cd /d "%~dp0"

echo ============================================
echo   烧录采集固件（不需要装任何东西）
echo ============================================
echo.
echo   [1] ESP32-S3-CAM  GOOUUU（N16R8）   ^<-- 你现在这块
echo   [2] ESP32-CAM     AI Thinker（旧板）
echo.
set /p BOARD=选板型 [回车=1]:
if "%BOARD%"=="" set BOARD=1

if "%BOARD%"=="1" (
  set CHIP=esp32s3
  set BIN=firmware\capture-s3-goouuu-merged.bin
  set SIZE=16MB
  set DESC=ESP32-S3
) else (
  set CHIP=esp32
  set BIN=firmware\capture-esp32cam-merged.bin
  set SIZE=4MB
  set DESC=ESP32
)

if not exist "%BIN%" ( echo 缺少镜像 %BIN% & pause & exit /b 1 )

echo.
echo --- 当前串口 ---
powershell -NoProfile -Command "Get-CimInstance Win32_SerialPort | Select-Object DeviceID,Description | Format-Table -AutoSize"
echo.
echo S3 是原生 USB：插上就能烧，不需要 GPIO0 接地。
echo 若识别不到端口，按住 BOOT 再按一下 RST 进下载模式。
echo.
set /p COM=输入串口号 [回车=COM6]:
if "%COM%"=="" set COM=COM6

echo.
echo --- 开始烧录 %DESC% 到 %COM%（16MB 约 1~3 分钟）---
tools\esptool.exe --chip %CHIP% --port %COM% --baud 921600 --before default-reset --after hard-reset write-flash -z --flash-mode dio --flash-freq 80m --flash-size %SIZE% 0x0 "%BIN%"

if errorlevel 1 (
  echo.
  echo 烧录失败。常见原因：
  echo   1) 串口被占用（关掉串口助手 / Arduino IDE）
  echo   2) 没进下载模式：按住 BOOT 再按一下 RST
  echo   3) 换一根能传数据的 USB-C 线
  echo   4) S3 板请确认选的是 [1]
  echo.
  pause
  exit /b 1
)

echo.
echo ============ 烧录成功 ============
echo 按一下 RST，然后看串口（115200），期望：
echo    [CAM] freenove/s3-eye OK: 640x480 ...
echo    [WiFi] OK ip=192.168.x.x
echo.
pause