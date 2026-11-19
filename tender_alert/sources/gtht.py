"""国泰海通证券 · 供应商门户（supplier.gtht.com）。

国泰海通自己就是保险买家，每年 3 月前后集中采购财产一切险、公众责任险、董监高责任险、员工意外险。
用门户首页背后的公开接口 GET /api/index/notice：按标题关键词搜索，按时间倒序；无需登录。
noticeFlag=01 是采购公告，02 是采购结果公告。服务器用旧式 TLS 重协商，需要 LegacyTLSAdapter。
标题里常写「财产一切险」「责任险」而不写「保险」，所以服务端搜单字「险」，再由 filters.match 按关键词筛。
"""

import logging
from datetime import date

from ..config import Config
from ..models import Notice
from ._common import LegacyTLSAdapter, outside_shanghai

NAME = "国泰海通·供应商门户"
REGION_SCOPED = True  # 地区在本模块里按标题过滤
INCREMENTAL = True  # 全量存档时不带关键词，只抓上次成功之后的

log = logging.getLogger(__name__)

BASE = "https://supplier.gtht.com"
API = BASE + "/api/index/notice"
PAGE_SIZE = 30
MAX_PAGES = 5


def _search(session, flag: str, since: date, keyword: str | None) -> list[dict]:
    rows: list[dict] = []
    for page in range(1, MAX_PAGES + 1):
        resp = session.get(API, params={
            "noticeType": "01", "currentPage": page, "noticeFlag": flag, "pageSize": PAGE_SIZE,
            **({"title": keyword} if keyword else {}),
        }, headers={"Referer": BASE + "/"})
        resp.raise_for_status()
        body = resp.json()
        if body.get("code") != "0":
            raise RuntimeError(f"国泰海通接口返回失败：{body.get('msg')!r}")
        batch = body["data"].get("list") or []
        rows += batch
        if len(batch) < PAGE_SIZE or len(rows) >= (body["data"].get("total") or 0):
            return rows
        if min(r.get("publishTime") or "" for r in batch)[:10] < since.isoformat():
            return rows
    log.warning("noticeFlag=%s 结果超过 %d 页，只取了前 %d 条", flag, MAX_PAGES, len(rows))
    return rows


def fetch(session, cfg: Config, since: date) -> list[Notice]:
    session.mount("https://", LegacyTLSAdapter())
    flags = ["01"] + (["02"] if "result" in cfg.fetch_kinds else [])
    notices = []
    for flag in flags:
        # 全量存档时不带关键词，保险筛选交给 filters.match
        for rec in _search(session, flag, since, None if cfg.archive else "险"):
            title = (rec.get("title") or "").strip()
            if outside_shanghai(title):
                continue
            url = f"{BASE}/#/procurnotice?id={rec['id']}&projectId={rec.get('projectId') or ''}&noticeFlag=true"
            notices.append(Notice(
                source="gtht",
                source_id=rec["id"],
                title=title,
                url=url,
                publish_date=rec.get("publishTime") or "",
                purchaser="国泰海通证券",
                region="上海",
                kind="result" if flag == "02" else "other",  # 采购公告里再按标题细分
            ))
    return notices
