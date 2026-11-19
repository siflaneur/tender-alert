"""上海申通地铁 · 采购电子商务平台（eps.shmetro.com，MSCP 系统）。

平台的公告对公众可见：打开 https://eps.shmetro.com/ 首页（不用登录）就能看到
招标公告、单一来源公示、中标候选人/结果、竞价公告等栏目各自最新的 10 条。
但它的数据接口要求 `sign` 头（前端 JS 里用内嵌私钥做 SHA256withRSA 签名），
直接抓接口会被拒绝（「签名错误：签名为null」）。

所以这个来源**不碰接口、也不复刻签名**，而是用本机 Chrome/Edge 的无头模式渲染公开首页
（`--headless --dump-dom`），再从渲染后的 DOM 里读公告标题和日期——等于用浏览器正常打开
一次首页。注意：该站 robots.txt 写的是整站 `Disallow: /`，**用户已于 2026-10-31 明确授权
忽略它**（见 CLAUDE.md / HANDOFF.md 的规则例外），只用于个人提醒。

局限：
- 每个栏目只有最新 10 条，没有公开的历史列表/搜索；靠每天定时运行增量覆盖。
  申通公告量小（招标公告每天约 1–2 条），每天跑一次基本不会漏。
- 列表里的标题没有详情链接，邮件链接指向平台首页。
- 依赖本机装有 Chrome 或 Edge；页面结构变化时会报错（来源连续失败会发告警邮件）。
"""

import hashlib
import logging
import os
import re
import shutil
import subprocess
import tempfile
from datetime import date, datetime

from bs4 import BeautifulSoup

from ..config import Config
from ..models import Notice, classify_kind

NAME = "上海申通地铁·采购电子商务平台"
REGION_SCOPED = True  # 平台上全是上海地铁系统的单位

log = logging.getLogger(__name__)

HOME = "https://eps.shmetro.com/"
# 平台只在每天 7:00–19:00 开放，其他时间首页只有运营时间提示，这时跳过、不算失败
OPEN_HOURS = (7, 19)
# 无头浏览器渲染时等页面数据加载完；页面本身是 SPA，公告靠接口异步加载
VIRTUAL_TIME_BUDGET_MS = 25000
RENDER_TIMEOUT = 120

# 栏目名 → (公告类型, 归一化类型)。不在表里的栏目按 other 留给主流程归类
_TABS = {
    "招标公告": ("招标公告", "tender"),
    "单一来源公示": ("单一来源公示", "tender"),
    "竞价公告": ("竞价公告", "tender"),
    "揭榜询比采购公告": ("揭榜询比采购公告", "tender"),
    "中标候选人公示": ("中标候选人公示", "result"),
    "中标结果公告": ("中标结果公告", "result"),
    "竞价结果": ("竞价结果", "result"),
    "揭榜询比采购结果公告": ("揭榜询比采购结果公告", "result"),
}

_CONTENT_ID_RE = re.compile(r"^(?:tagContent(?:Mj)?|openBiddingContent)\d+$")
_DATE_RE = re.compile(r"\[(\d{4})(\d{2})(\d{2})\]")

_CHROME_CANDIDATES = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
)


def _browser() -> str:
    for path in _CHROME_CANDIDATES:
        if path and os.path.isfile(path):
            return path
    for name in ("chrome.exe", "chrome", "msedge.exe", "msedge"):
        found = shutil.which(name)
        if found:
            return found
    raise RuntimeError("找不到 Chrome 或 Edge，申通地铁来源需要本机浏览器渲染页面")


def _render() -> str:
    """用无头浏览器打开申通地铁首页，返回渲染后的 DOM。"""
    exe = _browser()
    profile = tempfile.mkdtemp(prefix="tender-alert-shmetro-")
    cmd = [
        exe,
        "--headless=new",
        "--disable-gpu",
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-extensions",
        "--mute-audio",
        f"--user-data-dir={profile}",
        f"--virtual-time-budget={VIRTUAL_TIME_BUDGET_MS}",
        "--dump-dom",
        HOME,
    ]
    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            timeout=RENDER_TIMEOUT,
            creationflags=creationflags,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"申通地铁首页渲染超时（>{RENDER_TIMEOUT} 秒）") from None
    finally:
        shutil.rmtree(profile, ignore_errors=True)

    html = proc.stdout.decode("utf-8", errors="replace")
    if "MSCP系统" not in html or "tagContent" not in html:
        detail = proc.stderr.decode("utf-8", errors="replace")[-300:] if proc.stderr else ""
        raise RuntimeError(f"申通地铁首页渲染失败（退出码 {proc.returncode}）：{detail}")
    return html


def _tab_label(container) -> str | None:
    """列表容器对应的栏目名（往上找 el-tab-pane / el-tabs 配对）。"""
    pane = container.find_parent(class_="el-tab-pane")
    group = pane.find_parent(class_="el-tabs") if pane else None
    if pane is None or group is None:
        return None
    pane_id = pane.get("id")
    tab = group.select_one(f'[aria-controls="{pane_id}"]') if pane_id else None
    if tab is None:  # 有的版本没有 aria-controls，按组内顺序反查
        panes = group.find_all(class_="el-tab-pane")
        tabs = group.select(".el-tabs__item")
        if pane not in panes or len(panes) != len(tabs):
            return None
        tab = tabs[panes.index(pane)]
    return tab.get_text(strip=True)


def _source_id(title: str, publish_date: str) -> str:
    return hashlib.md5(f"{title}|{publish_date}".encode("utf-8")).hexdigest()[:16]


def _parse(html: str) -> list[Notice]:
    """从渲染后的首页 DOM 里提取各栏目的公告。结构对不上时抛异常（由主流程记失败）。"""
    soup = BeautifulSoup(html, "lxml")
    found: dict[str, Notice] = {}
    for container in soup.find_all("div", id=_CONTENT_ID_RE):
        label = _tab_label(container)
        if label is None:
            log.warning("申通地铁有个公告列表找不到对应栏目，跳过：%s", container.get("id"))
            continue
        notice_type, kind = _TABS.get(label, (label, "other"))
        for tr in container.select("tr"):
            link = tr.select_one("td.newstd1 a")
            if link is None:
                continue
            title = re.sub(r"\s+", " ", link.get_text(" ", strip=True)).strip()
            date_cell = tr.select_one("td.newstd2")
            m = _DATE_RE.search(date_cell.get_text(strip=True)) if date_cell else None
            if not title or m is None:
                continue
            publish_date = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
            row_kind = kind
            if row_kind == "tender":
                title_kind = classify_kind(None, title)
                if title_kind in ("correction", "result"):
                    row_kind = title_kind
            sid = _source_id(title, publish_date)
            found.setdefault(sid, Notice(
                source="shmetro",
                source_id=sid,
                title=title,
                url=HOME,  # 列表里没有详情链接，链接到平台首页
                publish_date=publish_date,
                notice_type=notice_type,
                region="上海",
                kind=row_kind,
            ))
    if not found:
        raise RuntimeError("申通地铁首页里没有解析出任何公告，可能是网站改版了")
    return list(found.values())


def _now() -> datetime:
    return datetime.now()  # 本机就在上海时区


def fetch(session, cfg: Config, since: date) -> list[Notice]:
    now = _now()
    if not OPEN_HOURS[0] <= now.hour < OPEN_HOURS[1]:
        log.info("现在 %s 不在平台开放时间 %d:00–%d:00 内，本次跳过", now.strftime("%H:%M"), *OPEN_HOURS)
        return []
    return _parse(_render())
