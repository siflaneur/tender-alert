#!/usr/bin/env bash
# Linux 下手动或定时运行一次招标提醒。参数原样透传，例如：./run.sh --dry-run -v
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

export TZ="${TZ:-Asia/Shanghai}"
export PYTHONIOENCODING="utf-8"

PYTHON="$SCRIPT_DIR/.venv/bin/python"
if [[ ! -x "$PYTHON" ]]; then
  echo "找不到 $PYTHON，请先运行 bash deploy/setup-linux.sh 初始化虚拟环境" >&2
  exit 1
fi

exec "$PYTHON" -m tender_alert "$@"
