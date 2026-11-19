"""上海证券交易所 · 采购门户（www.sse.com.cn/purchase）。

上交所和下属公司（技术公司、信息公司、数据公司、指数公司、证券博物馆等）的采购只发在这里，
不走政府采购，也不在上海公共资源交易平台上。保险类每年有几条：财产保险、公务用车车险、
员工商业补充医疗保险（2026-11-11 调研时首页就有 3 条，之前都没抓到）。

「公开征集」栏目是静态网页，第一页约 100 条、覆盖四五个月，每次只取这一页。
标题格式是「供应商征集公告（项目名）」，另有每月一条「采购意向公开（N月）」。
「结果公示」栏目的数据由 query.sse.com.cn 的接口加载，这一版没有接。
"""

import logging
import re
from datetime import date
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ..config import Config
from ..models import Notice

NAME = "上海证券交易所·采购门户"
REGION_SCOPED = True  # 上交所及下属公司都在上海

log = logging.getLogger(__name__)

LIST_URL = "https://www.sse.com.cn/purchase/information/"
_ID_RE = re.compile(r"c_\d{8}_(\d+)\.shtml")
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _parse(html: str) -> list[Notice]:
    soup = BeautifulSoup(html, "lxml")
    notices = []
    for dd in soup.select("div.sse_list_1 dl dd"):
        a = dd.find("a", href=True)
        span = dd.find("span")
        day = _DATE_RE.search(span.get_text() if span else "")
        if a is None or day is None:
            continue
        url = urljoin(LIST_URL, a["href"])
        m = _ID_RE.search(url)
        title = (a.get("title") or a.get_text()).strip()
        if not title:
            continue
        notices.append(Notice(
            source="sse",
            source_id=m.group(1) if m else url,
            title=title,
            url=url,
            publish_date=day.group(0),
            purchaser="上海证券交易所",
            region="上海",
            kind="other",  # 主流程按标题归类：「征集」算招标，「采购意向」算意向
        ))
    if not notices:
        raise RuntimeError("上交所采购门户的公告列表一条也没解析出来，可能是网站改版了")
    return notices


def fetch(session, cfg: Config, since: date) -> list[Notice]:
    resp = session.get(LIST_URL)
    resp.raise_for_status()
    resp.encoding = "utf-8"
    notices = _parse(resp.text)
    recent = [n for n in notices if n.publish_date >= since.isoformat()]
    if len(recent) == len(notices):
        log.warning("上交所采购门户第一页全是 %s 之后的公告，更早的可能没取到", since)
    return recent
