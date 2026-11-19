"""上海机场集团 · 阳光采购招标公告（www.shanghaiairport.com/zhaobiao）。

列表页是 Vue 空壳，公告走公开 POST 接口 `/AvinexApi/Pc/SunshineProcurementHandler.aspx`：
服务端按标题关键字（key）和年份（time）筛选，分页返回 pid、标题、发布日期、投标截止时间。
无需登录、没有验证码；站点在加速乐 CDN 后面，先正常打开一次列表页拿 Cookie 再调接口。
标题里的保险类公告很集中（建安工程一切险、财产险、车辆保险等），约 2–4 条/年。

详情页（/zhaobiao/info_itemid_{pid}.html）对新公告返回 404 或「暂无数据」，所以邮件里
先链接到招标公告列表页，点进去按标题找。网站声明「未经许可禁止转载」：这里只做个人提醒，
邮件里只有标题和链接，不转载正文。
"""

import logging
from datetime import date

from ..config import Config
from ..models import Notice

NAME = "上海机场集团·阳光采购"
REGION_SCOPED = True  # 机场集团及下属单位都在上海
INCREMENTAL = True  # 全量存档时不带关键字列出全部公告，只抓上次成功之后的

log = logging.getLogger(__name__)

BASE = "https://www.shanghaiairport.com"
LIST_URL = BASE + "/zhaobiao/index.html"
API = BASE + "/AvinexApi/Pc/SunshineProcurementHandler.aspx"
PAGE_SIZE = 100
MAX_PAGES = 3
# 服务端按标题包含关键字搜；单字「险」覆盖保险/车险/责任险/一切险等，
# 「医疗」覆盖补充医疗、医疗保障，「保障」覆盖「员工综合福利保障」这类标题
SEARCH_TERMS = ("险", "医疗", "保障")


def _notice(rec: dict) -> Notice:
    return Notice(
        source="airport",
        source_id=str(rec["pid"]),
        title=(rec.get("title") or "").strip(),
        url=LIST_URL,  # 详情页对新公告不可用，链接到列表页
        publish_date=(rec.get("createDate") or "")[:10],
        region="上海",
        # 接口不区分公告类型，交给主流程按标题归类：「XX中标结果公示」应归为结果而不是招标
        kind="other",
        extra={"deadline": (rec.get("subTitle2Date") or "")[:16] or None},
    )


def _search(session, term: str, since: date | None = None) -> list[dict]:
    rows: list[dict] = []
    for page in range(1, MAX_PAGES + 1):
        resp = session.post(
            API,
            data={"action": "GetTenderList", "pageIndex": page, "pageSize": PAGE_SIZE,
                  "key": term, "time": ""},
            headers={
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "X-Requested-With": "XMLHttpRequest",
                "Origin": BASE,
                "Referer": LIST_URL,
            },
        )
        resp.raise_for_status()
        try:
            body = resp.json()
        except ValueError as exc:
            raise RuntimeError("机场集团招标接口返回的不是 JSON，可能改版了") from exc
        if not body.get("success"):
            raise RuntimeError(f"机场集团招标接口返回失败：{body.get('message')!r}")
        try:
            data = body["data"]
            batch = data.get("items") or []
            total = int(data.get("totalItems") or 0)
        except (KeyError, TypeError) as exc:
            raise RuntimeError("机场集团招标接口返回格式变了") from exc
        rows += batch
        if len(batch) < PAGE_SIZE or len(rows) >= total:
            return rows
        if since and min((r.get("createDate") or "")[:10] for r in batch) < since.isoformat():
            return rows  # 按时间倒序，已经翻出回看范围
    log.warning("机场集团「%s」的结果超过 %d 页，后面的公告没有抓取", term, MAX_PAGES)
    return rows


def fetch(session, cfg: Config, since: date) -> list[Notice]:
    session.headers["Accept"] = "application/json, text/javascript, */*; q=0.01"
    session.get(LIST_URL, headers={"Referer": BASE + "/"})  # 像正常访客一样先打开列表页
    found: dict[str, Notice] = {}
    # 全量存档时关键字留空列出全部公告（一天两三条），保险筛选交给 filters.match
    searches = [("", since)] if cfg.archive else [(term, None) for term in SEARCH_TERMS]
    for term, until in searches:
        for rec in _search(session, term, until):
            try:
                notice = _notice(rec)
            except KeyError:
                raise RuntimeError("机场集团招标接口返回的记录里没有 pid") from None
            if not notice.title or not notice.publish_date:
                continue
            found.setdefault(notice.source_id, notice)
    return list(found.values())
