"""上海政府采购网 www.zfcg.sh.gov.cn（政采云平台）。

用网站自己搜索页背后的 JSON 接口 POST /portal/all：服务端按标题关键词、公告类型、
发布时间筛选，按时间倒序返回。站内全是上海的公告，不用再按地区筛。
没有 robots.txt，也没有验证码或反爬。

全量存档模式（config.toml 的 [archive]）下改用 POST /portal/category：不带关键词，按公告类型列出全部公告，
按时间倒序翻到 since 之前就停（since 由主流程按上次成功时间增量计算），保险筛选交给 filters.match。
"""

import logging
import re
from datetime import date, datetime, timedelta, timezone
from urllib.parse import quote

from ..config import Config
from ..models import Notice

NAME = "上海政府采购网"
REGION_SCOPED = True
INCREMENTAL = True  # 全量存档时按类型抓全部列表，只抓上次成功之后的

log = logging.getLogger(__name__)

BASE = "https://www.zfcg.sh.gov.cn"
CST = timezone(timedelta(hours=8))
PAGE_SIZE = 15  # 接口每页最多返回 15 条
MAX_PAGES = 10

# 每种公告类型（secondCode）单独查一次。标题搜单字「险」能同时查到保险/车险/意外险/责任险，
# 「风险」「抢险」之类的噪音由 filters.match 按关键词去掉。
# 采购合同公告每天几十条「车辆保险框架协议合同」，没有提醒价值，不查。
QUERIES = {
    "tender": [
        ("ZcyAnnouncement2", "采购公告"),
        ("ZcyAnnouncement1", "单一来源采购公示"),
        ("77-993319", "框架协议公告"),
        ("77-987173", "非政府采购项目招标公告"),
    ],
    "correction": [
        ("ZcyAnnouncement3", "采购更正公告"),
        ("77-212547", "非政府采购项目更正公告"),
    ],
    "result": [
        ("ZcyAnnouncement4", "采购结果公告"),
        ("ZcyAnnouncement6", "采购终止（废标）公告"),
        ("77-298537", "非政府采购项目结果公告"),
    ],
}
# 采购意向的标题很笼统（「XX局2027年08月政府采购意向」），只能全文搜「保险」，结果按相关度排序
INTENT_QUERY = ("77-379845", "采购意向公开")

# 全量存档时按类型抓全部公告（POST /portal/category，不带关键词，按时间倒序，一页最多 100 条）。
# 不抓「网上超市公告」（77-334307）：是网上商城的小额下单记录，量大、没有招标价值。
ALL_CATEGORIES = [
    ("ZcyAnnouncement2", "采购公告"),
    ("ZcyAnnouncement1", "单一来源采购公示"),
    ("ZcyAnnouncement3", "采购更正公告"),
    ("ZcyAnnouncement4", "采购结果公告"),
    ("ZcyAnnouncement6", "采购终止（废标）公告"),
    ("ZcyAnnouncement5", "采购合同公告"),
    ("77-379845", "采购意向公开"),
    ("77-993319", "框架协议公告"),
    ("77-596425", "履约验收公告"),
    ("77-987173", "非政府采购项目招标公告"),
    ("77-212547", "非政府采购项目更正公告"),
    ("77-298537", "非政府采购项目结果公告"),
]
LIST_PAGE_SIZE = 100
LIST_MAX_PAGES = 20

_TAG_RE = re.compile(r"<[^>]+>")


def _text(s: str | None) -> str:
    return re.sub(r"\s+", " ", _TAG_RE.sub("", s or "").replace("&nbsp;", " ")).strip()


def _ms(ms, fmt: str = "%Y-%m-%d %H:%M") -> str | None:
    if not ms:
        return None
    return datetime.fromtimestamp(int(ms) / 1000, tz=CST).strftime(fmt)


def _budget(raw: str | None) -> str | None:
    """「2500000元」→「250万元」。"""
    if not raw:
        return None
    m = re.fullmatch(r"([\d.]+)\s*元", raw.strip())
    if not m:
        return raw
    yuan = float(m.group(1))
    return f"{yuan / 10000:g}万元" if yuan >= 10000 else f"{yuan:g}元"


def _detail_url(rec: dict) -> str:
    url = f"{BASE}/site/detail?categoryCode={rec.get('firstCode') or 'ZcyAnnouncement'}"
    if rec.get("parentId"):
        url += f"&parentId={rec['parentId']}"
    # articleId 是类 base64 字符串，可能含 + / =
    return url + "&articleId=" + quote(rec["articleId"], safe="")


def _search(session, second_code: str, keyword: str, title_search: bool, since: date) -> list[dict]:
    begin = datetime.combine(since, datetime.min.time(), tzinfo=CST)
    # 有些公告的发布时间是当天稍晚的预约时间，结束时间往后放一天
    end = datetime.now(CST) + timedelta(days=1)
    rows: list[dict] = []
    for page in range(1, MAX_PAGES + 1):
        resp = session.post(
            BASE + "/portal/all",
            json={
                "keyword": keyword,
                "firstCode": "ZcyAnnouncement",
                "secondCode": second_code,
                "districtCode": [],
                "publishDateBegin": int(begin.timestamp() * 1000),
                "publishDateEnd": int(end.timestamp() * 1000),
                "pageNo": page,
                "pageSize": PAGE_SIZE,
                "isTitleSearch": 1 if title_search else 0,
                "order": "desc",
                "leaf": 0,
            },
            headers={
                "Accept": "application/json, text/plain, */*",
                "Origin": BASE,
                "Referer": BASE + "/site/search",
            },
        )
        resp.raise_for_status()
        body = resp.json()
        if not body.get("success"):
            raise RuntimeError(f"/portal/all 返回失败：{body.get('error')!r}")
        data = body["result"]["data"]
        batch = data.get("data") or []
        rows += batch
        if len(batch) < PAGE_SIZE or len(rows) >= (data.get("total") or 0):
            return rows
    log.warning("%s 结果超过 %d 页，只取了前 %d 条", second_code, MAX_PAGES, len(rows))
    return rows


def _notice(rec: dict, label: str) -> Notice:
    path = [p.strip() for p in (rec.get("pathName") or "").split("|") if p.strip()]
    return Notice(
        source="zfcg_sh",
        source_id=rec["articleId"],
        title=_text(rec.get("title")),
        url=_detail_url(rec),
        publish_date=_ms(rec.get("publishDate")) or "",
        purchaser=rec.get("purchaseName") or None,
        notice_type=path[-1] if path else label,
        region=rec.get("districtName") or None,
        extra={
            "budget": _budget(rec.get("budgetPrice")),
            "deadline": _ms(rec.get("bidOpeningTime")),
        },
    )


def _list(session, code: str, since: date) -> list[dict]:
    """某一类公告的全部列表，按时间倒序翻到 since 之前就停。"""
    rows: list[dict] = []
    for page in range(1, LIST_MAX_PAGES + 1):
        resp = session.post(
            BASE + "/portal/category",
            json={"pageNo": page, "pageSize": LIST_PAGE_SIZE, "categoryCode": code, "isGov": True},
            headers={"Accept": "application/json, text/plain, */*", "Origin": BASE, "Referer": BASE + "/site/category"},
        )
        resp.raise_for_status()
        body = resp.json()
        if not body.get("success"):
            raise RuntimeError(f"/portal/category 返回失败：{body.get('error')!r}")
        batch = body["result"]["data"].get("data") or []
        rows += batch
        if len(batch) < LIST_PAGE_SIZE:
            return rows
        if (_ms(batch[-1].get("publishDate"), "%Y-%m-%d") or "") < since.isoformat():
            return rows
    log.warning("%s 列表超过 %d 页，只取了前 %d 条", code, LIST_MAX_PAGES, len(rows))
    return rows


def fetch(session, cfg: Config, since: date) -> list[Notice]:
    notices: dict[str, Notice] = {}
    if cfg.archive:
        for code, label in ALL_CATEGORIES:
            for rec in _list(session, code, since):
                n = _notice(rec, label)
                if n.publish_date[:10] >= since.isoformat():
                    notices.setdefault(n.source_id, n)
        if "intent" in cfg.kinds:  # 采购意向的标题很笼统，要提醒的话仍需全文搜「保险」
            for rec in _search(session, INTENT_QUERY[0], "保险", False, since):
                n = _notice(rec, INTENT_QUERY[1])
                n.match_text = _text(rec.get("content"))
                notices[n.source_id] = n
        return list(notices.values())

    queries = [(code, label, "险", True) for kind in cfg.fetch_kinds for code, label in QUERIES.get(kind, [])]
    if "intent" in cfg.fetch_kinds:
        queries.append((*INTENT_QUERY, "保险", False))
    for code, label, keyword, title_search in queries:
        for rec in _search(session, code, keyword, title_search, since):
            n = _notice(rec, label)
            if not title_search:
                n.match_text = _text(rec.get("content"))
            notices.setdefault(n.source_id, n)
    return list(notices.values())
