# 国内 Linux 云服务器部署指南（tender-alert）

本文用于把本地笔记本上运行的 `tender-alert` 平滑迁移到国内云服务器，实现 7×24 小时无人值守托管。

---

## 1. 服务器选购建议

- **首选推荐**：**阿里云 ECS 经济型 e 实例（2核 2G、3M 固定带宽、40G 系统盘）**——新老同享 **99 元/年且续费同价**（约 8.25 元/月）。
- **地域选择**：优先选 **华东2（上海）**，访问上海本地政府与国企招标网站延迟最低。
- **操作系统**：**Ubuntu 24.04 LTS**（或 Debian 12）。
- **内存说明**：项目里的申通地铁（`shmetro`）来源需要无头 Chromium 渲染首页，建议至少 **2G 内存**（若选 1G 机器可配 2G swap，或在 `config.toml` 中关闭 `shmetro`）。

---

## 2. 在服务器上初始化环境

SSH 登录服务器后执行：

```bash
git clone https://github.com/siflaneur/tender-alert.git ~/tender-alert
cd ~/tender-alert
bash deploy/setup-linux.sh
```

脚本会自动完成：
1. 将系统时区设为 `Asia/Shanghai`（北京时间）
2. 安装 `python3`、`python3-venv`、中文字体及无头 `chromium`
3. 使用国内镜像源创建 `.venv` 并安装 `requirements.txt`
4. 跑完全部单元测试验证运行环境

> 如果系统用 `snap` 或自定义路径安装了 Chrome/Chromium，可在 `.env` 或 shell 环境中设置 `CHROME_BIN=/你的浏览器路径`。

---

## 3. 从本地笔记本无缝迁移状态（避免重发旧公告）

在**本地笔记本的 PowerShell** 中，将 `.env` 与现有数据库 `data/tender-alert.db` 上传到云服务器（把 `<服务器IP>` 和 `<用户名>` 换成你的实际值，例如 `root` 或 `ubuntu`）：

```powershell
# 先在服务器上建好 data 目录（如果还没有）
ssh <用户名>@<服务器IP> "mkdir -p ~/tender-alert/data"

# 上传邮箱配置与数据库（保留已提醒记录、翻页水位和全量存档）
scp .\.env <用户名>@<服务器IP>:~/tender-alert/.env
scp .\data\tender-alert.db <用户名>@<服务器IP>:~/tender-alert/data/tender-alert.db
```

> 迁移前建议确保本地没有正在运行的定时任务，避免拷贝到写入一半的数据库。

---

## 4. 试运行验收（不发信、不改库）

回到服务器 SSH，执行试运行并检查 17 个来源是否都能连通（尤其是 `shmetro` 无头 Chromium 与各上海站点）：

```bash
cd ~/tender-alert
chmod 600 .env
./run.sh --dry-run -v
```

如果邮箱配置也想验证一次，可运行：

```bash
./run.sh --test-mail
```

---

## 5. 启用云端定时任务并停用本地定时任务

试运行确认无误后：

1. **在云服务器上注册每天 10:00、15:00、22:00 的定时任务**（自带 45 分钟超时保护与 `data/run.lock` 互斥锁）：

```bash
cd ~/tender-alert
bash deploy/setup-linux.sh --install-cron
```

2. **在本地笔记本停用 Windows 定时任务**，防止两边同时跑导致重复发邮件：

```powershell
Disable-ScheduledTask -TaskName TenderAlert
```

（如果想彻底删除本地任务：`Unregister-ScheduledTask -TaskName TenderAlert -Confirm:$false`）

---

## 6. 日常运维与升级

- **查看实时日志**：
  ```bash
  tail -n 100 -f ~/tender-alert/logs/tender-alert.log
  ```
- **手动执行一次正式抓取**：
  ```bash
  cd ~/tender-alert && ./run.sh
  ```
- **更新代码**：本地开发提交并 `git push` 后，在服务器执行：
  ```bash
  cd ~/tender-alert && git pull
  # 若 requirements.txt 有变动，补跑一次：
  ./.venv/bin/pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
  ```
- **定期备份数据库**（可按需加一条每周备份 cron，或偶尔用 `scp` 拉回本地）：
  ```bash
  mkdir -p ~/tender-alert-backups
  cp ~/tender-alert/data/tender-alert.db ~/tender-alert-backups/tender-alert-$(date +%Y%m%d).db
  ```
