"""上海公共资源交易平台 · 综合采购（www.shggzy.com/jyxxwzcg）。

机场、地铁、磁浮、医院、街镇等单位的非政府采购，保险类公告大多在这里。

这个栏目没有开放接口，网站的搜索（/search/）和详情页（/jyxx…/…）都被 robots.txt 禁止抓取。
日常只读不在禁止范围内的列表页（如 /jyxxwzcggg、/jyxxwzcggg_2.jhtml）：
- 从最新一条开始按顺序往后翻，和在浏览器里点「下一页」一样，翻到上次看过的位置就停；
- 不跳页（跳页会弹滑块验证码），也不尝试通过验证码，遇到就报错停下；
- 只按列表上的标题筛选，不打开详情页。邮件里的详情链接是给人点的。

列表只能翻 10 页，采购公告 10 页只够 1.5 天，开始运行之前发布的公告翻不到。
所以经用户确认（2026-10-30），第一次正式运行时用网站搜索按关键词补齐回看范围内的历史，
之后不再使用搜索。--dry-run 不做补抓。

从第 2 页起和搜索都要带 cExt 令牌，令牌取自「综合采购」总列表第 1 页的网页，跟网站自己的脚本一样。
"""

import logging
import re
from datetime import date

from bs4 import BeautifulSoup

from ..config import Config
from ..models import Notice

NAME = "上海公共资源交易平台·综合采购"
REGION_SCOPED = True  # 平台上全是上海的单位
STATEFUL = True

log = logging.getLogger(__name__)

BASE = "https://www.shggzy.com"
HOME = BASE + "/jyxxwzcg"
SEARCH = BASE + "/search/queryContents.jhtml"
SEARCH_CHANNEL = "299"  # 综合采购
MIN_INTERVAL = 4.0  # robots.txt 要求 Crawl-delay: 1，这里放宽到 4 秒
# 每次最多翻几页（每页 10 条）。网站只允许不验证翻到第 10 页，第 11 页起弹滑块验证码（2026-10-30 实测）
MAX_PAGES = 10
REMEMBER = 30  # 每个子栏目记住最新多少条，下次翻到其中任意一条就停

# 详情页路径前缀 →（公告类型, 归一化类型）。不认识的前缀（采购意向、资格预审等）交给 classify_kind 按标题判断
PATH_TYPES = {
    "jyxxwzcggg": ("采购公告", "tender"),
    "jyxxwzcgdyly": ("单一来源公示", "tender"),
    "jyxxwzcgbggg": ("变更公告", "correction"),
    "jyxxwzcgzbjg": ("成交结果公告", "result"),
    "zbhxrgs": ("中标候选人公示", "result"),
}
# 按要提醒的公告类型选要翻的列表（列表路径和详情页前缀相同）
CHANNELS = {
    "tender": ["jyxxwzcggg", "jyxxwzcgdyly"],
    "correction": ["jyxxwzcgbggg"],
    "result": ["jyxxwzcgzbjg"],
}

_PATH_RE = re.compile(r"window\.open\('(/[^'?]+)")
_TOKEN_RE = re.compile(r'id="cExt" name="cExt" value="([^"]*)"')
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_COUNT_RE = re.compile(r"showCount\s*=\s*(\d+)")


def _get(session, url: str, referer: str, params: dict | None = None) -> tuple[str, str]:
    resp = session.get(url, params=params, headers={"Referer": referer})
    resp.raise_for_status()
    if "访问验证" in resp.text and "allList" not in resp.text:
        raise RuntimeError("网站弹出了滑块验证码，本次停止（程序不会尝试通过验证码）")
    return resp.text, resp.url


def _token(html: str) -> str:
    m = _TOKEN_RE.search(html)
    return m.group(1) if m else ""


def _rows(html: str) -> list[tuple[str, str, str]]:
    """列表页、搜索结果页的每一行：(详情页路径, 标题, 发布日期)。"""
    rows = []
    for li in BeautifulSoup(html, "lxml").select("#allList > ul > li"):
        m = _PATH_RE.search(li.get("onclick", ""))
        spans = li.find_all("span", recursive=False)
        if not m or len(spans) < 4:
            continue
        day = spans[3].get_text(strip=True)
        if not _DATE_RE.fullmatch(day):
            raise RuntimeError(f"列表里的发布日期格式变了：{day!r}，可能是网站改版了")
        # 搜索结果里关键词套着 <font> 高亮，不能按标签加空格拼；有的标题里还夹着制表符
        title = re.sub(r"\s+", " ", spans[1].get_text()).strip()
        rows.append((m.group(1), title, day))
    return rows


def clean_title(raw: str) -> str:
    """列表标题是「项目名-项目名采购公告」，去掉前面重复的项目名。

    项目名里本身也可能有「-」；两半的空格有时不一致（「安置房 --工程」和「安置房--工程」），比较时忽略空格。
    """
    for i, ch in enumerate(raw):
        head, tail = raw[:i].replace(" ", ""), raw[i + 1:]
        if ch == "-" and head and tail.replace(" ", "").startswith(head):
            return tail
    return raw


def _content_id(path: str) -> str:
    return path.rstrip("/").rsplit("/", 1)[-1]


def _notice(path: str, title: str, day: str, token: str) -> Notice:
    notice_type, kind = PATH_TYPES.get(path.strip("/").split("/")[0], (None, "other"))
    return Notice(
        source="shggzy_zhcg",
        source_id=_content_id(path),
        title=clean_title(title),
        url=f"{BASE}{path}?cExt={token}&isIndex=",  # 详情页不带令牌是空白页；这个令牌约 3 周内有效
        publish_date=day,
        notice_type=notice_type,
        region="上海市",
        kind=kind,
    )


def _fetch_channel(session, channel: str, token: str, since: date, state: dict, max_pages: int) -> list[tuple]:
    label = PATH_TYPES[channel][0]
    seen = set(state.get(channel, []))
    rows: list[tuple[str, str, str]] = []
    referer = HOME
    for page in range(1, max_pages + 1):
        url = f"{BASE}/{channel}" if page == 1 else f"{BASE}/{channel}_{page}.jhtml"
        html, referer_next = _get(session, url, referer, None if page == 1 else {"cExt": token})
        batch = _rows(html)
        if not batch:
            if page == 1:
                raise RuntimeError(f"{label}列表第 1 页是空的，可能是网站改版了")
            break  # 翻到底了
        rows += batch
        token = _token(html) or token
        referer = referer_next
        if seen & {_content_id(p) for p, _, _ in batch}:
            break  # 翻到上次看过的位置
        if min(d for _, _, d in batch) < since.isoformat():
            break  # 已经超出回看范围
    else:
        if max_pages < MAX_PAGES:
            pass  # 刚用搜索补过历史，只是来记一下当前位置
        elif seen:
            log.warning("%s 翻了 %d 页还没翻到上次看过的位置，中间可能有遗漏", label, max_pages)
        else:
            log.info("%s 首次运行，只看了最新 %d 页（%s 至 %s）", label, max_pages, rows[-1][2], rows[0][2])
    newest = [_content_id(p) for p, _, _ in rows]
    state[channel] = list(dict.fromkeys(newest + list(state.get(channel, []))))[:REMEMBER]
    return rows


def _search(session, keyword: str, token: str, since: date) -> list[tuple]:
    """网站搜索：标题含关键词、发布日期在回看范围内。不带日期范围的话结果按相关度乱序。"""
    params = {
        "cgr": "", "title": keyword, "channelId": SEARCH_CHANNEL, "origin": "", "inDates": "", "ext": "",
        "timeBegin": since.isoformat(), "timeEnd": f"{date.today().isoformat()} 23:59:59",
        "ext1": "", "ext2": "", "ext3": "", "cExt": token,
    }
    rows: list[tuple[str, str, str]] = []
    for page in range(1, MAX_PAGES + 1):
        url = SEARCH if page == 1 else f"{BASE}/search/queryContents_{page}.jhtml"
        html, _ = _get(session, url, HOME, params)
        count = _COUNT_RE.search(html)
        if count is None:
            raise RuntimeError("搜索结果页里找不到结果数，可能是网站改版了")
        rows += _rows(html)
        if len(rows) >= int(count.group(1)) or page * 10 >= int(count.group(1)):
            return rows
    log.warning("搜索「%s」结果超过 %d 页，只取了前 %d 条", keyword, MAX_PAGES, len(rows))
    return rows


def fetch(session, cfg: Config, since: date, state: dict, dry_run: bool = False) -> list[Notice]:
    session.interval = max(session.interval, MIN_INTERVAL)
    session.headers["Accept"] = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
    html, _ = _get(session, HOME, BASE + "/")
    token = _token(html)
    if not token:
        raise RuntimeError("综合采购列表页里找不到翻页令牌，可能是网站改版了")

    notices: dict[str, Notice] = {}
    backfill = not state.get("backfilled")
    if backfill and dry_run:
        log.info("dry-run 不做一次性补抓，第一次正式运行时才会用网站搜索补齐最近 %d 天", cfg.lookback_days)
        backfill = False
    if backfill:
        for keyword in cfg.keywords:
            for path, title, day in _search(session, keyword, token, since):
                n = _notice(path, title, day, token)
                notices.setdefault(n.source_id, n)
        log.info("已用网站搜索补齐 %s 以来的历史（只做这一次），共 %d 条", since, len(notices))

    for kind in cfg.fetch_kinds:
        for channel in CHANNELS.get(kind, []):
            for path, title, day in _fetch_channel(session, channel, token, since, state, 1 if backfill else MAX_PAGES):
                n = _notice(path, title, day, token)
                notices.setdefault(n.source_id, n)
    if backfill:
        state["backfilled"] = date.today().isoformat()
    return list(notices.values())
