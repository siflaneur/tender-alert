"""金采网 · 中国金融集中采购网（www.cfcpn.com）。

全国银行、保险、证券、基金等金融机构的采购公告，免登录。用它补上在上海的金融机构：
交行、浦发、各银行上海分行、太保、银联、人民银行上海总部等（它们自己的门户要登录或接不了）。

列表接口 `POST /jcw/noticeinfo/noticeInfo/dataNoticeList`，和网页上的筛选条件一一对应：
areaProvince 地区、noticeTitle 标题，按发布时间倒序分页（beginPublishTime 起始日期带上了但接口不认，靠翻到回看起点就停）。
**接口有频率限制**：连续请求六七次以后返回 403（2026-11-11 第一次正式运行想补 30 天，翻到第 7 页被拒）。
所以每种查法最多翻 3 页、间隔 5 秒，一次运行最多 6 个请求；不补历史，只靠每天增量。中途被 403 就保留已经拿到的。
两种查法合起来才全：
  1. 地区 = 上海市（310000）：采购人注册在上海的，每天三四十条
  2. 标题含「上海」、地区不是上海的：外地注册的机构在上海的分支，如「北京银行上海分行」标的是北京。
     这种查法会带出「上海浦东发展银行XX分行」之类的外地项目，用 _elsewhere 去掉
不按「保险」搜：保险公司的名字里都带保险，搜出来全是它们自己买别的东西；保险筛选交给主流程
（filters.match 会先去掉机构名再匹配关键词）。

上海银行、国泰海通、光大证券、上海农商银行的公告这里也有，和它们自己的门户重复，靠跨站去重合并。
"""

import html
import logging
import re
from datetime import date

import requests

from ..config import Config
from ..models import Notice, classify_kind
from ._common import outside_shanghai

NAME = "金采网·金融集中采购网"
REGION_SCOPED = True  # 模块里已经筛成上海的
INCREMENTAL = True  # 全量列表，每次只从上次成功的前一天抓起

log = logging.getLogger(__name__)

BASE = "http://www.cfcpn.com/jcw"  # 网站本身只有 http
LIST_PAGE = BASE + "/sys/index/goUrl?url=modules/sys/login/list&column=qbgg"
API = BASE + "/noticeinfo/noticeInfo/dataNoticeList"
DETAIL = BASE + "/sys/index/goUrl?url=modules/sys/login/detail&column=1&searchVal={id}"
SHANGHAI = "310000"
PAGE_SIZE = 50
MAX_PAGES = 3  # 每种查法最多 3 页（约 3 天的量）；接口连续请求六七次就返回 403，不能多翻
MIN_INTERVAL = 5.0
SEARCH_TERMS = ("险", "医疗保障")  # 没开全量存档时只在上海地区内按标题搜这两个词

_BRANCH_RE = re.compile(r"分行|分公司|支行|支公司")
_CODE_RE = re.compile(r"^\d{2}[A-Z]{2}\d{6}[;；]\s*")  # 太保的公告标题前面带内部编号，如「26LH001862;」


def _query(session, since: date, page: int, **filters) -> tuple[list[dict], int]:
    data = {
        "noticeType": "", "pageSize": PAGE_SIZE, "pageNo": page, "noticeState": "1", "isValid": "1",
        "orderBy": "publish_time desc", "noticeContent": "", "briefContent": "", "noticeTitle": "",
        "purchaseName": "", "purchaseId": "", "categoryLabName": "", "areaProvince": "", "labelAllId": "",
        "beginPublishTime": since.isoformat(), "endPublishTime": "",
    }
    data.update(filters)
    resp = session.post(API, data=data, headers={"X-Requested-With": "XMLHttpRequest", "Referer": LIST_PAGE})
    resp.raise_for_status()
    try:
        body = resp.json()
        rows = body["rows"] or []
        total = int(body["total"])
    except (ValueError, KeyError, TypeError) as exc:
        raise RuntimeError("金采网列表接口返回格式变了") from exc
    return rows, total


def _search(session, since: date, label: str, **filters) -> list[dict]:
    rows: list[dict] = []
    for page in range(1, MAX_PAGES + 1):
        try:
            batch, total = _query(session, since, page, **filters)
        except requests.HTTPError as e:
            if page == 1 or e.response is None or e.response.status_code != 403:
                raise
            log.warning("金采网「%s」翻到第 %d 页被限流（403），保留前 %d 条", label, page, len(rows))
            return rows
        rows += batch
        if not batch or len(rows) >= total:
            return rows
        if min((r.get("publishTime") or "9")[:10] for r in batch) < since.isoformat():
            return rows  # 起始日期条件万一失效，翻到回看窗口之前也停
    log.info("金采网「%s」超过 %d 页，只取了最新 %d 条（接口有频率限制，不多翻）", label, MAX_PAGES, len(rows))
    return rows


def _elsewhere(title: str) -> bool:
    """标题含「上海」但其实是外地分支的项目，如「上海银行盐城分行」「上海浦东发展银行杭州分行」。

    看标题里第一个「分行/分公司/支行」前面是不是上海；outside_shanghai 的地名表不全（没有盐城、铁岭这些），兜底用。
    """
    m = _BRANCH_RE.search(title)
    if m and "上海" not in title[max(0, m.start() - 3):m.start()]:
        return True
    return outside_shanghai(title)


def _notice(row: dict) -> Notice | None:
    title = _CODE_RE.sub("", html.unescape(row.get("noticeTitle") or "").strip())
    nid = row.get("id")
    day = (row.get("publishTime") or "")[:16]
    if not title or not nid or not day:
        return None
    method = row.get("purchaseTypeName") or None
    # 「采购方式」经常和公告对不上（中标公告的方式写着「公开招标」），所以先看标题，标题看不出来再看采购方式
    kind = classify_kind(None, title)
    if kind == "other":
        kind = classify_kind(method, "")
    return Notice(
        source="cfcpn",
        source_id=nid,
        title=title,
        url=DETAIL.format(id=nid),
        publish_date=day,
        purchaser=row.get("userName") or None,
        notice_type=method,
        region=row.get("area") or None,
        kind=kind,
    )


def fetch(session, cfg: Config, since: date) -> list[Notice]:
    session.interval = max(session.interval, MIN_INTERVAL)
    found: dict[str, Notice] = {}

    def keep(rows: list[dict], check_title: bool) -> None:
        for row in rows:
            n = _notice(row)
            if n is None or n.publish_date[:10] < since.isoformat():
                continue
            if check_title and (row.get("areaProvince") == SHANGHAI or _elsewhere(n.title)):
                continue  # 上海地区的第一种查法已经有了；外地分支不要
            found.setdefault(n.source_id, n)

    if cfg.archive:
        keep(_search(session, since, "上海地区", areaProvince=SHANGHAI), check_title=False)
    else:
        for term in SEARCH_TERMS:
            keep(_search(session, since, f"上海地区·{term}", areaProvince=SHANGHAI, noticeTitle=term), check_title=False)
    keep(_search(session, since, "标题含上海", noticeTitle="上海"), check_title=True)
    return list(found.values())
