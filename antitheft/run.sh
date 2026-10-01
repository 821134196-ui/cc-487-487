#!/usr/bin/env bash
# 一条本地启动命令：导入漏拆/串台/盘差样例并启动 Web（含进程内闭店与提醒任务）
set -e
cd "$(dirname "$0")"
python3 -m app.seed data/antitheft.db evidence
exec python3 -m app.web --db data/antitheft.db --evidence evidence --port "${PORT:-8621}"
