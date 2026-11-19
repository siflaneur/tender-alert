"""东方航空 · 集采实施平台（caigou.ceair.com）。

东航集团及下属公司的招标、谈判、询比等公告，保险采购金额大（员工医疗保障、董责险、财产险、车辆险）。
用网站「最新商机」页背后的接口 POST /portal/portal/sysInfoList：按标题关键词搜索，按发布时间倒序。
robots.txt 为空，无需登录。页脚声明「不得转载」：这里只做个人提醒（标题 + 链接），不转载内容。

东航是全国性企业，外地分公司（山西、甘肃、北京大兴等）的采购按标题过滤掉。

接口有频率限制（连续搜 7 个关键词就会返回「操作频繁，请6分钟后重试」），所以只搜两次：
单字「险」（覆盖保险、车险、责任险、意外险……）和「医疗保障」（「员工综合医疗保障」这类标题不带「险」字），
再由 filters.match 按关键词筛；请求间隔至少 5 秒。
"""

import logging
from datetime import date

from ..config import Config
from ..models import Notice
from ._common import outside_shanghai

NAME = "东方航空·集采实施平台"
REGION_SCOPED = True  # 地区在本模块里按标题过滤
INCREMENTAL = True  # 全量存档时不带关键词列出全部公告，只抓上次成功之后的

log = logging.getLogger(__name__)

BASE = "https://caigou.ceair.com"
API = BASE + "/portal/portal/sysInfoList?siteFlag=false"
PAGE_SIZE = 50
MAX_PAGES = 5
MIN_INTERVAL = 5.0
SEARCH_TERMS = ["险", "医疗保障"]
# 全量存档时不带关键词列出全部公告：全国一天约 300 条，增量抓一次要翻好几页，
# 所以每页 100 条、间隔放宽到 8 秒；被限流就报错，下次运行从上次成功处补抓
ARCHIVE_PAGE_SIZE = 100
ARCHIVE_MAX_PAGES = 8
ARCHIVE_INTERVAL = 8.0


def _deadline(end_time: str | None) -> str | None:
    """「20261110133000」→「2026-11-10 13:30」。"""
    if not end_time or len(end_time) < 12:
        return None
    t = end_time
    return f"{t[:4]}-{t[4:6]}-{t[6:8]} {t[8:10]}:{t[10:12]}"


def _search(session, keyword: str, since: date, page_size: int = PAGE_SIZE, max_pages: int = MAX_PAGES) -> list[dict]:
    rows: list[dict] = []
    for page in range(1, max_pages + 1):
        resp = session.post(API, data={"pageNo": page, "sourceType": "", "title": keyword, "pageSize": page_size}, headers={
            "X-Requested-With": "XMLHttpRequest",
            "Referer": BASE + "/portal/portal/opportunity",
        })
        resp.raise_for_status()
        body = resp.json()
        if body.get("flag") != "T":
            raise RuntimeError(f"东航接口返回失败：{body.get('param1')!r}")
        result = body["param1"]
        batch = result.get("result") or []
        rows += batch
        if not batch or page >= (result.get("totalPages") or 1):
            return rows
        if min(r.get("publishTime") or "" for r in batch)[:10] < since.isoformat():
            return rows  # 结果按时间倒序，已经翻出回看范围
    log.warning("搜索「%s」结果超过 %d 页，只取了前 %d 条", keyword, max_pages, len(rows))
    return rows


def fetch(session, cfg: Config, since: date) -> list[Notice]:
    if cfg.archive:
        session.interval = max(session.interval, ARCHIVE_INTERVAL)
        searches = [("", ARCHIVE_PAGE_SIZE, ARCHIVE_MAX_PAGES)]  # 全量，保险筛选交给 filters.match
    else:
        session.interval = max(session.interval, MIN_INTERVAL)
        searches = [(term, PAGE_SIZE, MAX_PAGES) for term in SEARCH_TERMS]
    notices: dict[str, Notice] = {}
    for term, page_size, max_pages in searches:
        for rec in _search(session, term, since, page_size, max_pages):
            title = (rec.get("title") or "").strip()
            if outside_shanghai(title):
                log.debug("外地，跳过：%s", title)
                continue
            notices.setdefault(rec["id"], Notice(
                source="ceair",
                source_id=rec["id"],
                title=title,
                url=f"{BASE}/portal/portal/noticeDetails?id={rec['id']}",
                publish_date=(rec.get("publishTime") or "")[:16],
                purchaser="东方航空",
                region="上海",
                extra={"deadline": _deadline(rec.get("endTime"))},
            ))
    return list(notices.values())
