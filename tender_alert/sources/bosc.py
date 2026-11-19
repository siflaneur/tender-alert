"""上海银行 · 官网「招标采购」栏目（www.bosc.cn/zh/jrsh/xxgk_1091/tzzxgqk/zbcg/）。

上海银行自己就是保险买家（董监高责任险、员工补充医疗保险、信用卡权益保险等）。
栏目是静态网页，每页 20 条，按时间倒序，翻页是 index_1.shtml、index_2.shtml…；网站没有 robots.txt。
记住上次看到的公告，翻到那里就停，平时每次只看第 1 页。外地分行（天津、北京、宁波等）的采购按标题过滤掉。
"""

import logging
import re
from datetime import date
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ..config import Config
from ..models import Notice
from ._common import outside_shanghai

NAME = "上海银行·招标采购"
REGION_SCOPED = True  # 地区在本模块里按标题过滤
STATEFUL = True

log = logging.getLogger(__name__)

LIST_URL = "https://www.bosc.cn/zh/jrsh/xxgk_1091/tzzxgqk/zbcg/"
MAX_PAGES = 10
REMEMBER = 40

_ITEM_RE = re.compile(r"^\./\d{6}/t(\d{4})(\d{2})(\d{2})_(\d+)\.shtml$")


def _items(html: str, page_url: str) -> list[tuple[str, str, str, str]]:
    """列表页的每条公告：(编号, 标题, 发布日期, 详情页地址)。"""
    items = []
    for a in BeautifulSoup(html, "lxml").find_all("a", href=True):
        m = _ITEM_RE.match(a["href"])
        if m:
            title = (a.get("title") or a.get_text()).strip()
            items.append((m.group(4), title, f"{m.group(1)}-{m.group(2)}-{m.group(3)}", urljoin(page_url, a["href"])))
    return items


def fetch(session, cfg: Config, since: date, state: dict, dry_run: bool = False) -> list[Notice]:
    seen = set(state.get("seen", []))
    rows: list[tuple[str, str, str, str]] = []
    for page in range(MAX_PAGES):
        url = LIST_URL if page == 0 else f"{LIST_URL}index_{page}.shtml"
        resp = session.get(url)
        resp.raise_for_status()
        resp.encoding = "utf-8"
        batch = _items(resp.text, url)
        if not batch:
            if page == 0:
                raise RuntimeError("上海银行招标采购列表第 1 页一条公告也没解析出来，可能是网站改版了")
            break
        rows += batch
        if seen & {i for i, _, _, _ in batch} or min(d for _, _, d, _ in batch) < since.isoformat():
            break
    else:
        if seen:
            log.warning("翻了 %d 页还没翻到上次看过的位置，中间可能有遗漏", MAX_PAGES)
    state["seen"] = list(dict.fromkeys([i for i, _, _, _ in rows] + list(seen)))[:REMEMBER]

    notices = []
    for sid, title, day, url in rows:
        if outside_shanghai(title):
            log.debug("外地，跳过：%s", title)
            continue
        notices.append(Notice(
            source="bosc", source_id=sid, title=title, url=url, publish_date=day,
            purchaser="上海银行", region="上海",
        ))
    return notices
