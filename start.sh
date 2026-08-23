#!/usr/bin/env bash
# 启动流水线服务（Ctrl+C 停止）
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  echo "尚未初始化环境，请先运行 ./setup.sh"
  exit 1
fi

echo "启动流水线服务..."
echo "浏览器访问: http://127.0.0.1:5100"
echo ""
exec .venv/bin/python app.py
