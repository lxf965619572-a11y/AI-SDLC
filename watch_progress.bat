@echo off
chcp 65001 >nul
title 流水线进度监视（每15秒刷新，Ctrl+C 退出）
cd /d "%~dp0"
.venv\Scripts\python.exe scripts\watch_progress.py
pause
