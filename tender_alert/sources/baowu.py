"""中国宝武 · 宝华智慧招标共享平台（baohuabidding.cn）。

平台的公开全文检索接口可以按标题和发布日期筛选公告。平台覆盖宝武体系及部分上海国企，
但全国各地项目混在一起，所以只保留招标人、标题或项目地点明确属于上海的公告。
列表接口返回的正文摘要足以判断大部分外地项目；标题不明显的保险项目再打开详情页确认。
网站没有 robots.txt。

全量存档模式下不带标题条件抓全部公告（全国两天约 150 条）：保险项目照旧，看不出地区时打开详情页；
其他项目只凭标题和列表摘要（取 800 字）判断是不是上海，不额外请求，判断不出来的不存。
"""

import json
import logging
import re
from datetime import date, datetime
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ..config import Config
from ..models import Notice
from ._common import outside_shanghai

NAME = "中国宝武·宝华智慧招标共享平台"
REGION_SCOPED = False
INCREMENTAL = True  # 全量存档时不带标题条件，只抓上次成功之后的

log = logging.getLogger(__name__)

BASE = "https://www.baohuabidding.cn"
SEARCH_URL = BASE + "/inteligentsearch/rest/esinteligentsearch/getFullTextDataNew"
REFERER = BASE + "/shbhgjzb/jyxx/trade_notice.html"
PAGE_SIZE = 100
MAX_PAGES = 10
SEARCH_TERMS = ("险", "补充医疗", "医疗保障", "承保")

_DATE_RE = re.compile(r"^20\d{2}-\d{2}-\d{2}$")
_TYPE_RE = re.compile(r"\[([^\[\]]{2,20})\]")
_PURCHASER_RE = re.compile(
    r"(?:项目)?(?:招标人|采购人)(?:为|是|[:：])\s*([^\s。；;，,]{2,80})"
)
_LOCATION_RE = re.compile(
    r"(?:项目地点|建设地点|服务地点|实施地点|交货地点|项目所在地|工程地点)"
    r"\s*(?:为|是|[:：])\s*([^。；;\n]{1,160})"
)
_OUTSIDE_RE = re.compile(
    r"南京|德盛|鄂城|鄂州|湛江|马钢|马鞍山|梅山|太钢|太原|八钢|乌鲁木齐|"
    r"韶钢|韶关|重钢|重庆|武钢|武汉|云南|福建|罗源|新疆"
)
_SHANGHAI_NAMES = (
    "上海",
    "宝山钢铁",
    "宝钢股份",
    "中国宝武钢铁集团",
    "欧冶",
    "宝武集团财务",
    "宝武资源",
    "宝武原料",
    "宝武装备",
    "宝武碳业",
    "宝武水务",
    "宝武清洁能源",
    "宝地资产",
    "宝钢工程",
    "华宝投资",
    "华宝证券",
    "华宝信托",
)
_TYPE_WORDS = (
    "资格预审",
    "招标公告",
    "采购公告",
    "竞价",
    "比选",
    "磋商",
    "谈判",
    "询价",
    "变更",
    "澄清",
    "延期",
    "中标候选人",
    "中标结果",
    "成交",
    "流标",
    "废标",
    "终止",
    "撤项",
    "采购预告",
)


def _plain(html: str) -> str:
    return BeautifulSoup(html or "", "lxml").get_text(" ", strip=True)


def _post(session, term: str | None, since: date, today: date, start: int) -> tuple[int, list[dict]]:
    payload = {
        "token": "",
        "pn": start,
        "rn": str(PAGE_SIZE),
        "sdt": f"{since.isoformat()} 00:00:00",
        "edt": f"{today.isoformat()} 23:59:59",
        "wd": "",
        "inc_wd": "",
        "exc_wd": "",
        "fields": "",
        "cnum": "001",
        "sort": '{"istop":"0","ordernum":"0","webdate":"0","id":"0"}',
        "ssort": "",
        "cl": 800 if term is None else 200,  # 全量时要更长的摘要来判断是不是上海项目
        "terminal": "",
        "condition": [
            {
                "fieldName": "categorynum",
                "equal": "018",
                "notEqual": None,
                "equalList": None,
                "notEqualList": None,
                "isLike": True,
                "likeType": 2,
            },
        ],
        "time": [],
        "highlights": "",
        "statistics": None,
        "unionCondition": None,
        "accuracy": "",
        "noParticiple": "1",
        "searchRange": None,
        "noWd": True,
    }
    if term is not None:
        payload["condition"].append({
            "fieldName": "searchtitle", "equal": term, "notEqual": None, "equalList": None,
            "notEqualList": None, "isLike": True, "likeType": 0,
        })
    resp = session.post(
        SEARCH_URL,
        data=json.dumps(payload, ensure_ascii=False),
        headers={
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Referer": REFERER,
        },
    )
    resp.raise_for_status()
    resp.encoding = "utf-8"
    try:
        outer = json.loads(resp.text)
        content = outer["content"]
        inner = json.loads(content) if isinstance(content, str) else content
        result = inner["result"]
        total = int(result["totalcount"])
        records = result["records"]
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("宝华招标接口返回格式变了") from exc
    if str(outer.get("code")) != "200" or not isinstance(records, list):
        raise RuntimeError(f"宝华招标接口返回异常：{outer.get('msg') or outer.get('code')}")
    if total > 0 and not records:
        raise RuntimeError("宝华招标接口有结果数但一条公告也没返回")
    return total, records


def _search(session, term: str, since: date, today: date) -> list[dict]:
    rows: list[dict] = []
    for page in range(MAX_PAGES):
        start = page * PAGE_SIZE
        total, batch = _post(session, term, since, today, start)
        rows.extend(batch)
        if start + len(batch) >= total:
            break
        if not batch:
            raise RuntimeError("宝华招标分页提前返回空列表")
    else:
        log.warning("宝华招标「%s」超过 %d 页，后面的公告没有抓取", term, MAX_PAGES)
    return rows


def _notice_type(*values: str | None) -> str | None:
    for value in values:
        if not value:
            continue
        for candidate in reversed(_TYPE_RE.findall(value)):
            if any(word in candidate for word in _TYPE_WORDS):
                return candidate
    return None


def _purchaser(text: str) -> str | None:
    match = _PURCHASER_RE.search(text)
    return match.group(1).strip(" ：:，,") if match else None


def _is_target_org(value: str | None) -> bool:
    return bool(value) and any(name in value for name in _SHANGHAI_NAMES)


def _outside(title: str) -> bool:
    return outside_shanghai(title) or bool(_OUTSIDE_RE.search(title))


def _region_from_title(title: str) -> str | None:
    if _outside(title):
        return None
    return "上海" if _is_target_org(title) else None


def _region_from_detail(title: str, text: str, purchaser: str | None) -> str | None:
    if _outside(title):
        return None
    if _is_target_org(purchaser):
        return "上海"
    for match in _LOCATION_RE.finditer(text):
        location = match.group(1)
        if "上海" in location or "宝山区" in location:
            return "上海"
    return None


def _detail(session, url: str) -> tuple[str, str | None]:
    resp = session.get(url, headers={"Referer": REFERER})
    resp.raise_for_status()
    resp.encoding = "utf-8"
    text = re.sub(r"\s+", " ", BeautifulSoup(resp.text, "lxml").get_text(" ", strip=True))
    return text, _purchaser(text)


def _detail_url(link: str) -> str:
    if link.startswith(("http://", "https://")):
        return link
    path = link if link.startswith("/") else "/" + link
    if not path.startswith("/shbhgjzb/"):
        path = "/shbhgjzb" + path
    return urljoin(BASE, path)


def fetch(session, cfg: Config, since: date) -> list[Notice]:
    today = date.today()
    found: dict[str, Notice] = {}
    terms = [None] if cfg.archive else list(SEARCH_TERMS)  # None = 不带标题条件的全量
    for term in terms:
        for row in _search(session, term, since, today):
            title = (row.get("title") or "").strip()
            if not title:
                continue
            is_hit = not any(w in title for w in cfg.exclude) and any(k in title for k in cfg.keywords)
            if not cfg.archive and not is_hit:
                continue

            publish_date = (row.get("webdate") or row.get("infodate") or "")[:10]
            if not _DATE_RE.match(publish_date):
                log.warning("宝华招标公告日期无法识别，跳过：%s", title)
                continue
            url = _detail_url((row.get("linkurl") or "").strip())
            if url == BASE + "/shbhgjzb/":
                log.warning("宝华招标公告没有详情链接，跳过：%s", title)
                continue

            purchaser = None
            region = _region_from_title(title)
            if region is None and not _outside(title):
                if is_hit:  # 保险项目看不出地区时打开详情页确认
                    text, purchaser = _detail(session, url)
                else:  # 其他项目只凭列表摘要判断，不额外请求
                    text = _plain(row.get("content") or "")
                    purchaser = _purchaser(text)
                region = _region_from_detail(title, text, purchaser)
            if region != "上海":
                continue

            source_id = str(row.get("id") or row.get("zbdlguid") or url)
            found[source_id] = Notice(
                source="baowu",
                source_id=source_id,
                title=title,
                url=url,
                publish_date=publish_date,
                purchaser=purchaser,
                notice_type=_notice_type(row.get("customtitle"), title),
                region=region,
            )
    return list(found.values())
