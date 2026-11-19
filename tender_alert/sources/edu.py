"""上海高校的采购/招标公告（各校采购与招标管理部门的网站）。

高校达到政府采购限额的项目已经在上海政府采购网、中国政府采购网里；这里补的是只发在学校自己网站上的
校内采购（竞价、询价、比选、校内招标等）。保险类很少，主要给全量存档用。

一校一个网站，但绝大多数是同一类静态列表页（苏迪 WebPlus 的 list.htm、博达的 info/…htm 等）：
一行一条，有标题链接和日期。所以这里用一个通用解析器 parse_list，学校只在 SCHOOLS 里登记
「名称 + 列表页地址」。每次每个地址只取第一页：一天三次运行，足够跟上学校的发布量。

上海交大、上海工程技术大学用的是同一套「数字化采购平台」（思必得 sfw_cms），列表由公开的 JSON 接口加载，
见 PLATFORM_SCHOOLS 和 fetch_platform。

单个学校失败（网站临时打不开、改版）只记警告，不影响其他学校；失败的学校达到三分之一才整体报错。
接不了的学校（列表靠接口加载、要登录等）见 HANDOFF.md。
"""

import logging
import re
from datetime import date
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from ..config import Config
from ..models import Notice, classify_kind
from ._common import decode_html

NAME = "上海高校·采购招标公告"
REGION_SCOPED = True  # 都是上海的学校

log = logging.getLogger(__name__)

# (key, 学校, [列表页地址, ...])。key 用在 source_id 里，定了就不要改，否则会重复存档。
SCHOOLS: list[tuple[str, str, list[str]]] = [
    ("fudan", "复旦大学", ["https://czzx.fudan.edu.cn/"]),  # 首页汇总了各栏目的最新公告
    ("tongji", "同济大学", ["https://czb.tongji.edu.cn/sggl/cgw/index.jsp"]),
    ("ecnu", "华东师范大学", ["https://cgxx.ecnu.edu.cn/cggghw/index.chtml", "https://cgxx.ecnu.edu.cn/jggghw/index.chtml"]),
    ("ecust", "华东理工大学", ["https://czzx.ecust.edu.cn/"]),
    ("dhu", "东华大学", ["https://ztb.dhu.edu.cn/"]),
    ("sufe", "上海财经大学", ["https://zhaobiao.sufe.edu.cn/20281/list.htm", "https://zhaobiao.sufe.edu.cn/20282/list.htm"]),
    ("shisu", "上海外国语大学", ["https://osaa.shisu.edu.cn/662/list.htm"]),
    ("shanghaitech", "上海科技大学", ["https://www.shanghaitech.edu.cn/1428/list.htm", "https://www.shanghaitech.edu.cn/1526/list.htm"]),
    ("shnu", "上海师范大学", ["https://zcc.shnu.edu.cn/23119/list.htm"]),
    ("shu", "上海大学", ["https://bidding.shu.edu.cn/sy/xtgg_index.jsp"]),
    ("shou", "上海海洋大学", ["https://ztb.shou.edu.cn/9350/list.htm"]),
    ("shutcm", "上海中医药大学", ["https://www.shutcm.edu.cn/249/list.htm"]),
    ("ecupl", "华东政法大学", ["https://xxgk.ecupl.edu.cn/cgztb/list.htm"]),
    ("suibe", "上海对外经贸大学", ["https://www.suibe.edu.cn/zhtb/zbgg/list.htm"]),
    ("shiep", "上海电力大学", ["https://sysyzcglc.shiep.edu.cn/313/list.htm"]),
    ("sit", "上海应用技术大学", ["https://czb.sit.edu.cn/cggg.htm"]),
    ("sspu", "上海第二工业大学", ["https://zc.sspu.edu.cn/1310/list.htm"]),
    ("sdju", "上海电机学院", ["https://zbb.sdju.edu.cn/"]),
    ("shupl", "上海政法学院", ["https://www.shupl.edu.cn/sbc/cggl/list.htm", "https://xxgk.shupl.edu.cn/wsgs_3881/list.htm"]),
    ("shcmusic", "上海音乐学院", ["https://www.shcmusic.edu.cn/1526/list.htm"]),
    ("sta", "上海戏剧学院", ["https://zt.sta.edu.cn/4563/list.htm", "https://zt.sta.edu.cn/4564/list.htm"]),
    ("sumhs", "上海健康医学院", ["https://www.sumhs.edu.cn/11321/list.htm"]),
    ("sbs", "上海商学院", ["https://zcc.sbs.edu.cn/gg/index.htm"]),
    ("shcc", "上海海关学院", ["https://czc.shcc.edu.cn/371/list.htm", "https://hbc.shcc.edu.cn/cggg/list.htm"]),
    ("sppc", "上海出版印刷高等专科学校", ["https://www.sppc.edu.cn/zbgg/list.htm"]),
    ("sitsh", "上海旅游高等专科学校", ["https://www.sitsh.edu.cn/zbgg_500/list.htm"]),
    # 海事大学一个栏目一个列表：招标公告、比选公告、单一来源公示、中标结果公示、更正公告。行里只写「11月03日」，年份从链接里取
    ("shmtu", "上海海事大学", [f"https://info.shmtu.edu.cn/{n}/list.htm" for n in (12378, 12379, 12380, 12381, 12382)]),
]

# 用「数字化采购平台」的学校：(key, 学校, 网站, 列表页参数, [每次查询附加的条件, ...])。
# 先正常打开列表页，再调它的列表接口；条件照抄网页上各栏目自己发的参数。
PLATFORM_SCHOOLS: list[tuple[str, str, str, str, list[list[tuple[str, str]]]]] = [
    ("sjtu", "上海交通大学", "https://pboffice.sjtu.edu.cn", "page=cms.psms.gglist&typeDetail=XQ&cate=103249", [
        [("type", "ZCXQ"), ("type", "YQXQ"), ("type", "BYXQ"), ("type", "CSXQ"), ("categoryId", "103249"), ("ggType", "XQ")],  # 采购公告
        [("type", "ZCGG"), ("type", "ZCGS"), ("type", "FBGG"), ("type", "BGGG"), ("categoryId", "103250"), ("ggType", "GG")],  # 成交公告
    ]),
    ("sues", "上海工程技术大学", "https://qlcdzhcg.sues.edu.cn", "page=cms.psms.gglist&typeDetail=XQ&orgType=A", [[]]),  # 不带条件就是全部
]
PLATFORM_LIMIT = 50
# 平台的公告类型代码：标题看不出类型时用
_PLATFORM_KINDS = {"ZCGS": "tender", "BGGG": "correction", "ZCGG": "result", "FBGG": "result", "YXGK": "intent"}
_OPS_RE = re.compile(r'ops_uuid\s*=\s*"([0-9a-f]+)"')
_URL_DATE_RE = re.compile(r"/(?:t)?(20\d{2})/?(\d{2})(\d{2})(?:_|/)")  # 文章地址里的发布日期：/2026/1103/ 或 /t20261118_

_DATE_RES = [
    re.compile(r"(20\d{2})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})"),
    re.compile(r"(?<!\d)(\d{1,2})\s+(20\d{2})\s*[-/.]\s*(\d{1,2})(?!\d)"),  # 同济、上财：「09 2026-11」，日在前
    re.compile(r"(?<!\d)(\d{1,2})\s*[-/.]\s*(\d{1,2})\s+(20\d{2})(?!\d)"),  # 应用技术大学：「11-09 2026」，年在后
    re.compile(r"(?<!\d)(2\d)-(\d{2})-(\d{2})(?!\d)"),  # 上海黄金交易所：「26-11-18」，两位年份
]
_TITLE_CLASS = re.compile(r"title|tit|bt", re.I)
# 标题里要有采购招标的字样，免得把导航、新闻、制度文件当成公告
_WANT = re.compile(r"采购|招标|中标|成交|询价|比选|磋商|竞价|谈判|单一来源|遴选|征集|比价|询比|流标|废标|结果公[示告]|候选人|"
                   r"需求调查|需求公示|征求意见|邀请公告")
_SKIP = re.compile(r"办法|规定|细则|制度|指南|流程图?$|模板|值班|培训|须知|下载|操作手册|工作会|管理中心$|办公室$|通知$|的通知|调研|党支部|"
                   r"^(?:更多|首页|采购公告|招标公告|中标公告|结果公告|成交公告|采购信息|招标信息|采购意向公开?|政府采购意向公开)$")
_ROW_TAGS = ("li", "tr", "dd", "dl", "div", "p", "td")


def _find_date(text: str) -> str | None:
    for i, rx in enumerate(_DATE_RES):
        m = rx.search(text)
        if m:
            a, b, c = m.groups()
            y, mo, d = ((a, b, c), (b, c, a), (c, a, b), ("20" + a, b, c))[i]
            try:
                return date(int(y), int(mo), int(d)).isoformat()
            except ValueError:
                continue
    return None


def _title(a) -> str:
    """链接的标题：优先 title 属性（正文常被截成「…」），整行都在链接里时取里面的标题元素。"""
    text = re.sub(r"\s+", " ", a.get_text(" ", strip=True))
    attr = re.sub(r"\s+", " ", a.get("title") or "").strip()
    if attr and not attr.startswith(("/", "http")):  # 二工大的 title 属性里放的是网址
        return attr
    inner = a.find(class_=_TITLE_CLASS) or a.find(["h1", "h2", "h3", "h4", "h5", "em"])
    if inner is not None:
        inner_text = re.sub(r"\s+", " ", inner.get_text(" ", strip=True))
        if len(inner_text) >= 6:
            return inner_text
    titled = a.find(attrs={"title": True})  # 中烟平台：完整标题在里面某个 span 的 title 属性里
    if titled is not None and len(titled["title"].strip()) >= 6:
        return re.sub(r"\s+", " ", titled["title"]).strip()
    if _find_date(text):  # 链接里标题和日期挨在一起（中银国际：<p>标题</p><span>日期</span>），取带采购字样的那一块
        for el in a.find_all(["p", "span", "div"]):
            part = re.sub(r"\s+", " ", el.get_text(" ", strip=True))
            if len(part) >= 6 and _WANT.search(part) and not _find_date(part):
                return part
    return text


def _row_date(node, title: str, whole_row: bool = False) -> str | None:
    """在一行里找发布日期。先看短小的元素（日期一般单独放在 span/div 里，有的还拆成「日」和「年月」两块），
    免得把摘要里的截止日期、标题里的日期当成发布日期。"""
    for el in node.find_all(True):
        text = el.get_text(" ", strip=True)
        if len(text) <= 24 and text not in title:
            day = _find_date(text)
            if day:
                return day
    if whole_row:
        return None
    return _find_date(node.get_text(" ", strip=True).replace(title, " "))


def parse_list(html: str, base_url: str, stop_before: str | None = None) -> list[tuple[str, str, str]]:
    """从列表页里取出 (标题, 链接, 日期)。日期在标题链接所在的那一行里找，找不到的不要。

    给了 stop_before（YYYY-MM-DD）时，针对 SMG 这类单页几千条全历史按时间倒序排列的页面，
    连续遇到 40 条早于 stop_before 的公告就提前停下（留足余量兼容顶部少量置顶旧公告）。
    """
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style"]):
        tag.decompose()
    items: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    old_streak = 0
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("javascript", "#", "mailto")):
            continue
        title = _title(a)
        if not 6 <= len(title) <= 150 or not _WANT.search(title) or _SKIP.search(title):
            continue
        day = _row_date(a, title, whole_row=True)  # 有的网站整行都包在链接里
        node = a
        for _ in range(4):  # 否则往上找「这一行」：最近的、文字不长、里面有日期的容器
            if day:
                break
            node = node.parent
            if node is None or node.name in ("body", "html"):
                break
            if node.name not in _ROW_TAGS:
                continue
            if len(node.get_text(" ", strip=True)) > len(title) + 120:
                break
            if len({x["href"] for x in node.find_all("a", href=True)}) > 2:
                break  # 已经是好几条公告的共同容器了，里面的日期不属于这一条
            day = _row_date(node, title)
        url = urljoin(base_url, href)
        if not day:  # 行里没有带年份的日期（海事大学只写「11月03日」）：用文章地址里的日期
            m = _URL_DATE_RE.search(url)
            day = _find_date("-".join(m.groups())) if m else None
        if day and url not in seen:
            seen.add(url)
            items.append((title, url, day))
            if stop_before:
                old_streak = old_streak + 1 if day[:10] < stop_before else 0
                if old_streak >= 40:
                    break
    return items


def _source_id(key: str, url: str) -> str:
    parts = urlsplit(url)
    return f"{key}:{parts.path}{'?' + parts.query if parts.query else ''}"


def fetch_school(session, key: str, school: str, urls: list[str], stop_before: str | None = None) -> list[Notice]:
    notices = []
    for list_url in urls:
        resp = session.get(list_url)
        resp.raise_for_status()
        for title, url, day in parse_list(decode_html(resp), list_url, stop_before=stop_before):
            notices.append(Notice(
                source="edu", source_id=_source_id(key, url), title=title, url=url, publish_date=day,
                purchaser=school, region="上海", kind="other",  # 主流程按标题归类
            ))
    if not notices:
        raise RuntimeError(f"{school}的公告列表一条也没解析出来，可能是网站改版了")
    return notices


def fetch_platform(session, key: str, school: str, base: str, page_qs: str,
                   queries: list[list[tuple[str, str]]]) -> list[Notice]:
    """「数字化采购平台」的学校：打开列表页（拿会话和页面里的 ops_uuid 参数），再调公开的列表接口。"""
    endpoint = base + "/sfw_cms/e"
    page_url = f"{endpoint}?{page_qs}"
    page = session.get(page_url)
    page.raise_for_status()
    ops = _OPS_RE.search(page.text)
    notices = []
    for extra in queries:
        data = [("window_", "json"), ("start", "1"), ("limit", str(PLATFORM_LIMIT)), ("filter", ""),
                ("sort", "beginTime desc"), *extra, ("keywords", "")]
        if ops:
            data.append(("ops_uuid", ops.group(1)))
        data += [("request_method_", "ajax"), ("browser_", "notmsie"), ("page", "cms.psms.publish.query")]
        resp = session.post(endpoint, data=data, headers={"X-Requested-With": "XMLHttpRequest", "Referer": page_url})
        resp.raise_for_status()
        try:
            rows = resp.json()["resultset"] or []
        except (ValueError, KeyError, TypeError) as exc:
            raise RuntimeError(f"{school}采购平台的列表接口返回格式变了") from exc
        for row in rows:
            title = re.sub(r"\s+", " ", row.get("subject") or "").strip()
            nid = row.get("id")
            day = (row.get("syncTime") or row.get("beginTime") or "")[:16]
            if not title or not nid or not day:
                continue
            if row.get("url"):
                url = row["url"]
            elif row.get("syncId"):
                url = f"{base}/provider/#/publish/{row['syncId']}"  # 网页上点标题打开的就是这个地址
            else:
                url = page_url
            kind = classify_kind(None, title)
            if kind == "other":  # 标题看不出来再看平台的类型代码（…XQ 是各种采购需求公告）
                code = row.get("type") or ""
                kind = _PLATFORM_KINDS.get(code, "tender" if code.endswith("XQ") else "other")
            notices.append(Notice(
                source="edu", source_id=f"{key}:{nid}", title=title, url=url, publish_date=day,
                purchaser=school, notice_type=row.get("shopTypeName") or None, region="上海", kind=kind,
                extra={"deadline": (row.get("endTime") or "")[:16] or None},
            ))
    if not notices:
        raise RuntimeError(f"{school}采购平台一条公告也没返回，可能是接口参数变了")
    return notices


def sub_name(sub_key: str) -> str | None:
    for key, school, _ in SCHOOLS:
        if key == sub_key:
            return school
    for row in PLATFORM_SCHOOLS:
        if row[0] == sub_key:
            return row[1]
    return None


def fetch(session, cfg: Config, since: date) -> list[Notice]:
    found: dict[str, Notice] = {}
    failed: list[str] = []
    sub_health: dict[str, tuple[str, str | None]] = {}
    jobs = [(key, school, fetch_school, (key, school, urls, since.isoformat())) for key, school, urls in SCHOOLS]
    jobs += [(row[0], row[1], fetch_platform, row) for row in PLATFORM_SCHOOLS]
    for sub_key, school, func, args in jobs:
        try:
            batch = func(session, *args)
        except Exception as e:  # 一个学校的网站出问题不影响别的学校，单独记连续失败次数
            err = f"{type(e).__name__}: {str(e)[:200]}"
            log.warning("%s 抓取失败：%s", school, err[:140])
            failed.append(school)
            sub_health[f"edu:{sub_key}"] = (school, err)
            continue
        sub_health[f"edu:{sub_key}"] = (school, None)
        for n in batch:
            if n.publish_date[:10] >= since.isoformat():
                found.setdefault(n.source_id, n)
    if len(failed) * 3 >= len(jobs):
        raise RuntimeError(f"{len(jobs)} 所高校里有 {len(failed)} 所抓取失败：{'、'.join(failed[:8])}")
    session.sub_health = sub_health
    return list(found.values())
