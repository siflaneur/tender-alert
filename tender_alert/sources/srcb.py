"""上海农商银行 · 供应商门户采购公告（pms2g.shrcb.com）。

采购公告由公开静态网页提供，按时间倒序，每页 10 条，翻页地址是 index_2.html、index_3.html……
网站没有 robots.txt。记住上次看到的公告，翻到那里就停；第一次运行则一直翻到回看窗口之外。
"""

import logging
import re
from datetime import date
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ..config import Config
from ..models import Notice

NAME = "上海农商银行·供应商门户"
REGION_SCOPED = True
STATEFUL = True

log = logging.getLogger(__name__)

BASE = "https://pms2g.shrcb.com"
LIST_URL = BASE + "/cms/cmscaigougg/index.html"
MAX_PAGES = 20
REMEMBER = 40

_ITEM_RE = re.compile(r"/cms/cmscaigougg/([0-9a-f]{32})\.html$")
_DATE_RE = re.compile(r"(20\d{2})/(\d{1,2})/(\d{1,2})")


def _items(html: str) -> list[tuple[str, str, str, str]]:
    """列表页的每条公告：(编号, 标题, 发布日期, 详情页地址)。"""
    soup = BeautifulSoup(html, "lxml")
    items = []
    for a in soup.select("div.List2 a[href]"):
        m = _ITEM_RE.search(a["href"])
        if not m:
            continue
        li = a.find_parent("li")
        if li is None:
            continue
        dm = _DATE_RE.search(li.get_text(" ", strip=True))
        if not dm:
            continue
        title = (a.get("title") or a.get_text(" ", strip=True)).strip()
        items.append((
            m.group(1),
            title,
            f"{dm.group(1)}-{int(dm.group(2)):02d}-{int(dm.group(3)):02d}",
            urljoin(BASE, a["href"]),
        ))
    return items


def fetch(session, cfg: Config, since: date, state: dict, dry_run: bool = False) -> list[Notice]:
    seen = set(state.get("seen", []))
    rows: list[tuple[str, str, str, str]] = []
    for page in range(MAX_PAGES):
        url = LIST_URL if page == 0 else urljoin(BASE, f"/cms/cmscaigougg/index_{page + 1}.html")
        resp = session.get(url)
        resp.raise_for_status()
        resp.encoding = "utf-8"
        batch = _items(resp.text)
        if not batch:
            if page == 0:
                raise RuntimeError("上海农商银行采购公告第 1 页一条公告也没解析出来，可能是网站改版了")
            break
        rows += batch
        if seen & {sid for sid, _, _, _ in batch} or min(day for _, _, day, _ in batch) < since.isoformat():
            break
    else:
        if seen:
            log.warning("翻了 %d 页还没翻到上次看过的位置，中间可能有遗漏", MAX_PAGES)

    state["seen"] = list(dict.fromkeys([sid for sid, _, _, _ in rows] + list(seen)))[:REMEMBER]
    return [
        Notice(
            source="srcb",
            source_id=sid,
            title=title,
            url=url,
            publish_date=day,
            purchaser="上海农商银行",
            region="上海",
        )
        for sid, title, day, url in rows
    ]
