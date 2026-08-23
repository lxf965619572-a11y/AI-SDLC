@echo off
chcp 65001 >nul
title AI-SDLC 环境安装
cd /d "%~dp0"

echo ============================================
echo   多智能体软件开发流水线 - 一键安装 (Windows)
echo ============================================
echo.

echo [1/4] 检测 Python...
where python >nul 2>nul
if errorlevel 1 (
  echo [错误] 未检测到 Python。请先安装 Python 3.10+：
  echo        https://www.python.org/downloads/
  echo        安装时务必勾选 "Add python.exe to PATH"
  pause
  exit /b 1
)
for /f "tokens=2" %%v in ('python --version 2^>^&1') do set PYVER=%%v
echo        已检测到 Python %PYVER%

echo [2/4] 创建虚拟环境 .venv ...
if exist .venv (
  echo        .venv 已存在，跳过创建
) else (
  python -m venv .venv
  if errorlevel 1 (
    echo [错误] 虚拟环境创建失败
    pause
    exit /b 1
  )
)

echo [3/4] 安装依赖（首次约 1-3 分钟，请耐心等待）...
.venv\Scripts\python.exe -m pip install --upgrade pip -q
.venv\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 (
  echo [错误] 依赖安装失败，请检查网络后重新运行本脚本
  pause
  exit /b 1
)

echo [4/4] 生成配置文件...
if not exist .env (
  copy .env.example .env >nul
  echo        已创建 .env —— 启动前请按需编辑：
  echo        - 离线演示：保持 LLM_MOCK=1，无需任何 key
  echo        - 真实模型：LLM_MOCK=0 并填写 LLM_API_KEY 等
) else (
  echo        .env 已存在，保留原配置
)

echo.
echo ============================================
echo   安装完成！
echo   下一步：
echo     1. 按需编辑 .env 配置 LLM
echo     2. 双击 start.bat 启动
echo ============================================
pause
