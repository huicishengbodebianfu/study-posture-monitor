@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
title ESP32-CAM 采集 (零安装)
cd /d "%~dp0"

echo ============================================
echo   ESP32-CAM 采图工具（不需要装任何东西）
echo ============================================
echo.

set /p IP=设备 IP（串口里打印的那个，如 192.168.1.23）:
if "%IP%"=="" ( echo 没填 IP & pause & exit /b 1 )

echo.
echo 姿态标签：
echo   1) upright  坐直
echo   2) slouch   低头/驼背
echo   3) lean     侧歪
echo   4) phone    玩手机
echo   5) sleep    趴桌
echo   6) away     离开座位
echo   7) unknown  故障帧（遮挡/过暗/朝天花板）
set /p SEL=选编号 [1-7]:
if "%SEL%"=="1" set LABEL=upright
if "%SEL%"=="2" set LABEL=slouch
if "%SEL%"=="3" set LABEL=lean
if "%SEL%"=="4" set LABEL=phone
if "%SEL%"=="5" set LABEL=sleep
if "%SEL%"=="6" set LABEL=away
if "%SEL%"=="7" set LABEL=unknown
if "%LABEL%"=="" ( echo 编号不对 & pause & exit /b 1 )

set /p CNT=采多少张 [回车=40]:
if "%CNT%"=="" set CNT=40

echo.
echo --- 开始：IP=%IP% 标签=%LABEL% 张数=%CNT% ---
echo.

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0capture.ps1" -Ip "%IP%" -Label "%LABEL%" -Count %CNT%

echo.
echo 采完后把 captures 里对应的文件夹整个发回去
pause
