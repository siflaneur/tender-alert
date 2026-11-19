# tender-alert 项目说明（给 Claude / agent）

上海保险招标提醒：抓取多个招标网站，筛出上海单位采购保险服务的公告，发邮件提醒。

**接手前先读 [HANDOFF.md](HANDOFF.md)**：里面有当前状态、代码结构、各来源的坑和待办。

## 改完代码必须提交并登记（多个 agent 轮流维护这个项目）

项目用 git 管理（主分支 main）。git 不在 PATH 里时，用 `C:\Program Files\Git\cmd\git.exe`。
- **开工前**：先运行 `git status` 和 `git log --oneline -10`，确认工作区是干净的，并看看别人最近改了什么。有别人没提交的改动时，先问用户，不要覆盖。
- **每完成一项改动**：跑测试，通过后提交一次。提交说明用中文，写清改了什么、为什么改。一个提交只做一件事。
- **提交说明最后一行必须署名**，格式固定为 `Agent: <工具名>（<模型名>）`，前面空一行，例如
  `Agent: Claude Code（Claude Opus 5.5）`、`Agent: Codex（DeepSeek）`。这是 git trailer 格式，
  可以用 `git log --format="%h %s  [%(trailers:key=Agent,valueonly,separator=%x2C )]"` 列出每个提交是谁做的。
  HANDOFF.md 变更记录里的「谁」也写同样的名字。
- **同时**在 HANDOFF.md 末尾的「变更记录」里追加一条。如果改动影响了 HANDOFF.md 里已有的描述（状态、来源要点、规则、待办），把那些地方也一起改掉。
- `data/`（数据库：已提醒记录、翻页进度、补抓标记）和 `.env`（邮箱密码）不进 git。数据库靠项目同级的 `../tender-alert-backups/` 里的快照保存，大改之前先打一个新快照。
- 不要 push 到任何远程仓库，也不要改写历史（不用 `reset --hard`、`push --force`、`rebase`），除非用户明确要求。

## 必须遵守

- 不绕过任何反爬措施：验证码、JS 挑战、IP 轮换、伪造 cookie、解密网站有意加密的接口数据，都不做。
- robots.txt 由 `tender_alert/robots.py` 在每个请求发出前**强制检查**，被禁止的请求不会发出、来源记为失败。用户授权的例外只能写在它的 `EXCEPTIONS` 里，**新增或放宽例外必须由用户本人确认**，不要为了让来源跑通去改白名单或绕过检查。
- robots.txt 禁止的路径要先问用户。综合采购的 `/search/` 只允许那一次历史补抓（2026-10-30 已经做过），不要再加新的用法。
- **申通地铁 eps.shmetro.com 是用户 2026-10-31 明确授权的 robots 例外**：该站 robots.txt 整站 `Disallow: /`，仍接入；
  实现方式是用本机 Chrome/Edge 无头渲染公开首页取公告标题，**不要复刻/破解它的接口 sign 签名，也不要登录**。
- 综合采购每个栏目每次最多翻 10 页（第 11 页起有验证码）；东航一次运行最多搜两次（有频率限制）。
- 发真实邮件、注册定时任务、安装软件、删除数据之前，都要先问用户。
- Python 一律用 `.\.venv\Scripts\python.exe`；改完代码要跑 `.\.venv\Scripts\python.exe -m unittest discover -s tests`。
- 用中文和用户沟通。
