"""光大证券 · 集中采购管理平台（purms.ebscn.com）。

业务公告页面公开，搜索表单可以直接按标题搜「险」；结果里既有保险项目，也有风险管理系统等误报，
所以只对命中配置关键词的公告继续打开公开详情页，尽量少发请求。
列表只展示招标截止时间，不展示发布日期；详情正文末尾通常有落款日期，解析不到时退回截止日期。
网站没有 robots.txt。

全量存档模式下不带关键词取最新 50 条：取过发布日期的公告缓存在 state 里，不再打开详情页；
非保险公告每次最多新开 20 个详情页，保险公告不受限，保证提醒不耽误。
"""

import logging
import re
from datetime import date
from urllib.parse import parse_qs, urljoin, urlsplit

from bs4 import BeautifulSoup

from ..config import Config
from ..models import Notice

NAME = "光大证券·集中采购管理平台"
REGION_SCOPED = True
STATEFUL = True

log = logging.getLogger(__name__)

BASE = "https://purms.ebscn.com"
SEARCH_URL = BASE + "/web/gdweb/index/businessAnnouncement.do"
MAX_RESULTS = 100
ARCHIVE_RESULTS = 50  # 全量存档时每次取最新 50 条
NEW_DETAILS_PER_RUN = 20  # 列表没有发布日期，要打开详情页才知道；非保险公告每次最多新开 20 个
DATE_CACHE_SIZE = 500

_DATE_RE = re.compile(r"(?<!\d)(20\d{2})[年./-](\d{1,2})[月./-](\d{1,2})日?")
_DEADLINE_RE = re.compile(r"(20\d{2})-(\d{2})-(\d{2})\D+(\d{2})\D+(\d{2})")


def _normalize_date(match: re.Match) -> str:
    return f"{match.group(1)}-{int(match.group(2)):02d}-{int(match.group(3)):02d}"


def _deadline(text: str) -> tuple[str | None, str | None]:
    m = _DEADLINE_RE.search(text)
    if not m:
        return None, None
    day = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    return day, f"{day} {m.group(4)}:{m.group(5)}"


def _items(html: str) -> list[dict]:
    """搜索结果的公开公告卡片。"""
    soup = BeautifulSoup(html, "lxml")
    container = soup.select_one("ul.ch_con")
    if container is None:
        raise RuntimeError("光大证券业务公告页结构变了，找不到公告列表")

    items = []
    for a in container.select("a.ch_bidding[href]"):
        title_node = a.select_one(".ch_bid_span")
        if title_node is None:
            continue
        article_id = (parse_qs(urlsplit(a["href"]).query).get("articleId") or [""])[0]
        if not article_id:
            continue
        deadline_day, deadline = _deadline(a.get_text(" ", strip=True))
        items.append({
            "id": article_id,
            "title": title_node.get_text(" ", strip=True),
            "url": urljoin(BASE, a["href"]),
            "deadline_day": deadline_day,
            "deadline": deadline,
        })
    return items


def _published(detail_html: str, fallback: str | None) -> str | None:
    """从详情正文取落款日期；落款日期晚于截止时间时视为不可信。"""
    soup = BeautifulSoup(detail_html, "lxml")
    detail = soup.select_one(".tender_detail")
    if detail is None:
        return fallback
    dates = [_normalize_date(m) for m in _DATE_RE.finditer(detail.get_text(" ", strip=True))]
    if not dates:
        return fallback
    newest = dates[-1]
    if fallback and newest > fallback:
        return fallback
    return newest


def fetch(session, cfg: Config, since: date, state: dict, dry_run: bool = False) -> list[Notice]:
    resp = session.post(
        SEARCH_URL,
        data={
            "pi.page": "1",
            "pi.pageSize": str(ARCHIVE_RESULTS if cfg.archive else MAX_RESULTS),
            "article.dealMode": "OPEN_INVITE",
            "article.columnCode": "INVITE_CODE",
            # 全量存档时不带关键词，取最新的一批；否则搜「险」
            "article.searchName": "" if cfg.archive else "险",
        },
        headers={"Referer": SEARCH_URL},
    )
    resp.raise_for_status()
    resp.encoding = "utf-8"
    items = _items(resp.text)

    cached = state.get("dates", {})
    dates: dict[str, str] = {}
    budget = NEW_DETAILS_PER_RUN  # 非保险公告每次最多新打开几个详情页，超出的下次再补
    skipped = 0
    notices = []
    for item in items:
        title = item["title"]
        is_hit = any(keyword in title for keyword in cfg.keywords)
        if not cfg.archive and not is_hit:
            continue

        published = cached.get(item["id"])
        if not published:
            if not is_hit:
                if budget <= 0:
                    skipped += 1
                    continue
                budget -= 1
            detail = session.get(item["url"], headers={"Referer": SEARCH_URL})
            detail.raise_for_status()
            detail.encoding = "utf-8"
            published = _published(detail.text, item["deadline_day"])
            if published is None:
                log.warning("光大证券公告既没有发布日期也没有截止日期，跳过：%s", title)
                continue
        dates[item["id"]] = published
        notices.append(Notice(
            source="ebscn",
            source_id=item["id"],
            title=title,
            url=item["url"],
            publish_date=published,
            purchaser="光大证券",
            region="上海",
            extra={"deadline": item["deadline"]},
        ))
    if skipped:
        log.info("还有 %d 条公告没取到发布日期，下次运行再补", skipped)
    # 记住所有取过日期的公告，以后不再打开详情页；只留最近的一批
    merged = {**cached, **dates}
    state["dates"] = dict(list(merged.items())[-DATE_CACHE_SIZE:])
    return notices
