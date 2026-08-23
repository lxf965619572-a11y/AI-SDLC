@echo off
chcp 65001 >nul
title 多智能体软件开发流水线
cd /d "%~dp0"

if not exist .venv (
  echo 尚未初始化环境，请先运行 setup.bat
  pause
  exit /b 1
)

echo 启动流水线服务（关闭本窗口即停止）...
echo 浏览器访问: http://127.0.0.1:5100
echo.
.venv\Scripts\python.exe app.py
pause
