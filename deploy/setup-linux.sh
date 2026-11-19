#!/usr/bin/env bash
# Ubuntu / Debian 云服务器一键初始化脚本（阿里云 ECS / 腾讯云轻量均适用）
# 用法：
#   bash deploy/setup-linux.sh                 # 初始化环境、装依赖与无头 Chromium、跑单元测试
#   bash deploy/setup-linux.sh --install-cron  # 同时写入每天 10:00 / 15:00 / 22:00 的 crontab 定时任务
set -euo pipefail

INSTALL_CRON=0
if [[ "${1:-}" == "--install-cron" ]]; then
  INSTALL_CRON=1
fi

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

SUDO=""
if [[ "${EUID:-$(id -u)}" -ne 0 ]] && command -v sudo >/dev/null 2>&1; then
  SUDO="sudo"
fi

echo "==> 1. 设置系统时区为 Asia/Shanghai（北京时间）"
if command -v timedatectl >/dev/null 2>&1; then
  $SUDO timedatectl set-timezone Asia/Shanghai || true
fi

echo "==> 2. 安装系统基础依赖（Python 3 / venv / 字体 / 无头浏览器）"
if command -v apt-get >/dev/null 2>&1; then
  $SUDO apt-get update -y
  $SUDO apt-get install -y python3 python3-venv python3-pip ca-certificates tzdata fonts-noto-cjk || true
  # 申通地铁来源需要本机 Chromium / Chrome 渲染公开首页；不同 Ubuntu/Debian 版本包名略有差异
  if ! command -v chromium >/dev/null 2>&1 && ! command -v chromium-browser >/dev/null 2>&1 && ! command -v google-chrome >/dev/null 2>&1; then
    if apt-cache show chromium >/dev/null 2>&1; then
      $SUDO apt-get install -y chromium
    elif apt-cache show chromium-browser >/dev/null 2>&1; then
      $SUDO apt-get install -y chromium-browser
    else
      echo "提示：apt 源里未找到 chromium 包，可手动安装 Chrome/Chromium，或在 config.toml 里关闭 shmetro。"
    fi
  fi
else
  echo "提示：当前系统不是 apt 系（Ubuntu/Debian），请手动确认已安装 python3、venv 和 chromium/google-chrome。"
fi

echo "==> 3. 创建 Python 虚拟环境并安装依赖"
python3 -m venv "$PROJECT_DIR/.venv"
"$PROJECT_DIR/.venv/bin/pip" install --upgrade pip -i https://pypi.tuna.tsinghua.edu.cn/simple
"$PROJECT_DIR/.venv/bin/pip" install -r "$PROJECT_DIR/requirements.txt" -i https://pypi.tuna.tsinghua.edu.cn/simple

chmod +x "$PROJECT_DIR/run.sh" "$PROJECT_DIR/deploy/setup-linux.sh"

echo "==> 4. 运行单元测试验证环境"
TZ=Asia/Shanghai "$PROJECT_DIR/.venv/bin/python" -m unittest discover -s tests

if [[ "$INSTALL_CRON" -eq 1 ]]; then
  echo "==> 5. 注册每天 10:00、15:00、22:00 的 crontab 定时任务（单次最长 45 分钟）"
  MARKER="# tender-alert-managed"
  CRON_CMD="0 10,15,22 * * * TZ=Asia/Shanghai /usr/bin/timeout 45m $PROJECT_DIR/run.sh >> $PROJECT_DIR/logs/cron.log 2>&1 $MARKER"
  mkdir -p "$PROJECT_DIR/logs"
  EXISTING="$(crontab -l 2>/dev/null || true)"
  FILTERED="$(printf '%s\n' "$EXISTING" | grep -v "$MARKER" || true)"
  {
    printf '%s\n' "$FILTERED"
    printf '%s\n' "$CRON_CMD"
  } | sed '/^[[:space:]]*$/d' | crontab -
  echo "已写入 crontab："
  crontab -l | grep "$MARKER"
else
  echo ""
  echo "初始化完成！接下来请："
  echo "  1) 上传 .env 与 data/tender-alert.db 到 $PROJECT_DIR"
  echo "  2) 试运行验证：./run.sh --dry-run -v"
  echo "  3) 验证无误后注册定时任务：bash deploy/setup-linux.sh --install-cron"
fi
