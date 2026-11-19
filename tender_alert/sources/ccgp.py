"""中国政府采购网 www.ccgp.gov.cn。

主要用来补上「在上海的中央单位」（如中国浦东干部学院、长江口航道管理局），
它们不在上海政府采购网发布。地方公告大多是上海政府采购网的转载，由去重逻辑处理。

用网站自己的搜索 search.ccgp.gov.cn/bxsearch：服务端按标题关键词「保险」、地区上海、
时间范围筛选。这个搜索有频率限制，所以每次运行先正常打开首页，再只搜一两页；
遇到「访问过于频繁」就报错，下次运行再查（时间范围覆盖整个回看窗口，不会漏）。
网站自己的链接都是 http，这里也用 http。
"""

import logging
import re
import time
from datetime import date, timedelta
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ..config import Config
from ..models import Notice

NAME = "中国政府采购网"
REGION_SCOPED = True  # 服务端已按 zoneId=31（上海）筛选
STATEFUL = True  # 独立记录保险搜索与在沪中央单位全量搜索各自的成功日期，被限流时不推进水位

log = logging.getLogger(__name__)

HOME = "http://www.ccgp.gov.cn/"
SEARCH_URL = "http://search.ccgp.gov.cn/bxsearch"
MAX_PAGES = 3  # 每页 20 条
ARCHIVE_MAX_PAGES = 6  # 在沪中央单位全量搜索：增量窗口约两天，3 页不够
MIN_INTERVAL = 3.0  # 这个站限流比较敏感，请求间隔至少 3 秒
ARCHIVE_INTERVAL = 5.0  # 全量存档时多一次搜索，间隔再放宽

_ID_RE = re.compile(r"/t\d{8}_(\d+)\.htm")
_TIME_RE = re.compile(r"(\d{4})\.(\d{2})\.(\d{2})\s+(\d{2}:\d{2})")
_BUYER_RE = re.compile(r"采购人：\s*([^|]+)")
_TOTAL_RE = re.compile(r"共找到\s*<span[^>]*>\s*(\d+)\s*</span>")


class RateLimited(RuntimeError):
    pass


def _clean(s: str | None) -> str | None:
    s = re.sub(r"\s+", " ", s or "").strip()
    return s or None


def _get(session, url: str, **kwargs) -> str:
    resp = session.get(url, **kwargs)
    resp.raise_for_status()
    resp.encoding = "utf-8"  # 页面是 UTF-8，但响应头没写 charset
    return resp.text


def _parse(html: str) -> tuple[list[Notice], int]:
    """解析搜索结果页。页面结构对不上时一律报错，免得网站改版后悄悄返回 0 条。"""
    if "访问过于频繁" in html:
        raise RateLimited("中国政府采购网搜索被限流（访问过于频繁），下次运行再试")
    total = _TOTAL_RE.search(html)
    if total is None:
        raise RuntimeError("搜索结果页里找不到结果数，可能是网站改版了")
    if int(total.group(1)) == 0:
        return [], 1
    soup = BeautifulSoup(html, "lxml")
    ul = soup.select_one("ul.vT-srch-result-list-bid")
    if ul is None:
        raise RuntimeError("搜索结果页里找不到结果列表，可能是网站改版了")
    notices = []
    for li in ul.find_all("li", recursive=False):
        a = li.find("a", href=True)
        if a is None:
            continue
        url = urljoin(HOME, a["href"])
        span = li.find("span")
        meta = span.get_text(" ", strip=True) if span else ""
        strong = span.find("strong") if span else None
        notice_type = _clean(strong.get_text()) if strong else None
        region = None
        if notice_type and notice_type in meta:
            # <br/> 后面是「公告类型 | 地区 | 品目」
            tail = [p for p in (_clean(x) for x in meta.split(notice_type, 1)[1].split("|")) if p]
            region = tail[0] if tail else None
        t = _TIME_RE.search(meta)
        if t is None:
            raise RuntimeError(f"解析不出发布时间，可能是网站改版了：{meta[:80]}")
        buyer = _BUYER_RE.search(meta)
        m = _ID_RE.search(url)
        notices.append(Notice(
            source="ccgp",
            source_id=m.group(1) if m else url,
            title=_clean(a.get_text()) or "",
            url=url,
            publish_date=f"{t.group(1)}-{t.group(2)}-{t.group(3)} {t.group(4)}",
            purchaser=_clean(buyer.group(1)) if buyer else None,
            notice_type=notice_type,
            region=region,
        ))
    if not notices:
        raise RuntimeError(f"结果数是 {total.group(1)}，但一条也没解析出来，可能是网站改版了")
    pages = re.search(r"Pager\(\{\s*size:\s*(\d+)", html)
    return notices, int(pages.group(1)) if pages else 1


def _search(session, kw: str, bid_sort: str, since: date, max_pages: int = MAX_PAGES) -> list[Notice]:
    notices: list[Notice] = []
    for page in range(1, max_pages + 1):
        html = _get(session, SEARCH_URL, headers={"Referer": HOME}, params={
            "searchtype": "1",  # 标题搜索
            "page_index": str(page),
            "bidSort": bid_sort,  # 0 中央 + 地方，1 中央，2 地方
            "buyerName": "",
            "projectId": "",
            "pinMu": "0",
            "bidType": "0",  # 全部公告类型，类型筛选交给 filters.match
            "dbselect": "bidx",
            "kw": kw,
            "start_time": since.strftime("%Y:%m:%d"),
            "end_time": date.today().strftime("%Y:%m:%d"),
            "timeType": "6",  # 指定时间范围
            "displayZone": "上海",
            "zoneId": "31",
            "pppStatus": "0",
            "agentName": "",
        })
        batch, pages = _parse(html)
        notices += batch
        if not batch or page >= pages:
            return notices
    log.warning("搜索「%s」结果超过 %d 页，只取了前 %d 条", kw or "全部", max_pages, len(notices))
    return notices


INSURANCE_LOOKBACK_DAYS = 7  # 保险搜索固定覆盖近 7 天（上海仅 1 页左右），防止某次漏页或隔天限流漏标


def fetch(session, cfg: Config, since: date, state: dict | None = None, dry_run: bool = False) -> list[Notice]:
    state = state if state is not None else {}
    today = date.today()
    session.interval = max(session.interval, ARCHIVE_INTERVAL if cfg.archive else MIN_INTERVAL)
    session.headers["Accept"] = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
    _get(session, HOME)  # 像正常访客一样先打开首页
    time.sleep(session.interval)  # 首页和搜索不在同一个域名，Session 的按域名限速管不到，手动停一下

    # 1) 保险专项搜索（中央 + 地方）：保持近 7 天窗口，不被全量存档的 1 天增量压短
    ins_since = max(since, today - timedelta(days=INSURANCE_LOOKBACK_DAYS)) if state.get("ins_last_ok") else since
    found: dict[str, Notice] = {}
    for n in _search(session, "保险", "0", ins_since):
        found.setdefault(n.source_id, n)
    state["ins_last_ok"] = today.isoformat()

    # 2) 全量存档时补「在上海的中央单位」全部公告（无关键词，覆盖车险/医责险等简称标）：
    #    单独记录成功日期：跨天首轮从上次成功前一天补抓，同日后续轮次只抓当天（避免两日叠加超 6 页截断）；
    #    若遇到限流则不更新 central_last_ok，下次运行自动把限流时段补回来。
    if cfg.archive:
        last_central = state.get("central_last_ok")
        if last_central:
            last_dt = date.fromisoformat(last_central)
            central_since = today if last_dt >= today else max(since, last_dt - timedelta(days=1))
        else:
            central_since = since
        try:
            central = _search(session, "", "1", central_since, ARCHIVE_MAX_PAGES)
        except RateLimited as e:
            log.warning("在沪中央单位搜索被限流，本次只保留保险搜索结果，下次从 %s 补抓：%s", central_since, e)
            central = []
        else:
            state["central_last_ok"] = today.isoformat()
        for n in central:
            found.setdefault(n.source_id, n)
    return list(found.values())
