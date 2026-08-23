#!/usr/bin/env bash
# 多智能体软件开发流水线 - 一键安装 (Linux / macOS)
set -e
cd "$(dirname "$0")"

echo "============================================"
echo "  多智能体软件开发流水线 - 一键安装"
echo "============================================"

echo "[1/4] 检测 Python..."
if ! command -v python3 >/dev/null 2>&1; then
  echo "[错误] 未检测到 python3，请先安装 Python 3.10+"
  exit 1
fi
echo "       已检测到 $(python3 --version)"

echo "[2/4] 创建虚拟环境 .venv ..."
[ -d .venv ] && echo "       .venv 已存在，跳过创建" || python3 -m venv .venv

echo "[3/4] 安装依赖（首次约 1-3 分钟）..."
.venv/bin/python -m pip install --upgrade pip -q
.venv/bin/python -m pip install -r requirements.txt

echo "[4/4] 生成配置文件..."
if [ ! -f .env ]; then
  cp .env.example .env
  echo "       已创建 .env —— 启动前请按需编辑："
  echo "       - 离线演示：保持 LLM_MOCK=1，无需任何 key"
  echo "       - 真实模型：LLM_MOCK=0 并填写 LLM_API_KEY 等"
else
  echo "       .env 已存在，保留原配置"
fi

echo ""
echo "============================================"
echo "  安装完成！下一步："
echo "    1. 按需编辑 .env 配置 LLM"
echo "    2. 运行 ./start.sh 启动"
echo "============================================"
