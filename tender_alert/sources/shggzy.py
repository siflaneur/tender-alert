"""上海公共资源交易平台 · 国企采购（gqcg.shggzy.com）。

上海市属、区属国企的招标采购，不在政府采购网发布。用子站自己的全文检索 JSON 接口：
服务端按标题关键词（多个关键词是「或」）和发布时间筛选，按时间倒序返回。
子站没有 robots.txt 限制，也不需要 cookie 或令牌。

主站 www.shggzy.com 的「综合采购」栏目另见 shggzy_zhcg.py。
"""

import json
import logging
import re
from datetime import date

from ..config import Config
from ..models import Notice

NAME = "上海公共资源交易平台·国企采购"
REGION_SCOPED = True  # 平台上全是上海的单位
INCREMENTAL = True  # 全量存档时不带关键词，只抓上次成功之后的

log = logging.getLogger(__name__)

BASE = "https://gqcg.shggzy.com"
API = BASE + "/inteligentsearch/rest/esinteligentsearch/getFullTextDataNew"
ROWS = 50
MAX_PAGES = 5

# projecthuanjie（项目环节）→（公告类型, 归一化类型）
_STAGES = {
    # 招标采购
    "001": ("招标计划", "intent"),
    "002": ("招标公告/资格预审公告", "tender"),
    "003": ("变更公告", "correction"),
    "004": ("中标候选人公示", "result"),
    "005": ("中标结果公告", "result"),
    "006": ("异常公告", "result"),
    # 非招标采购
    "007": ("采购公告", "tender"),
    "011": ("变更公告", "correction"),
    "008": ("预成交公告", "result"),
    "009": ("预成交公示", "result"),
    "012": ("成交公告", "result"),
    "010": ("采购异常", "result"),
}
_INTENT_CATEGORY = "002002005"  # 非招标采购-采购意向

# 采购人不是结构化字段，从正文开头里找
_PURCHASER_RE = re.compile(
    r"(?:招标人|采购人|招标单位|采购单位|项目单位)(?:为|是)?[:：\s]*"
    r"([一-龥A-Za-z0-9（）()·\-]{4,40}?(?:有限公司|有限责任公司|集团|公司|局|院|中心|委员会|办事处|学校|大学|医院|所|站))"
)


def _cond(field: str, value: str, like_type: int = 0) -> dict:
    return {"fieldName": field, "equal": value, "notEqual": None, "equalList": None,
            "notEqualList": None, "isLike": True, "likeType": like_type}


def _query(session, keywords: list[str] | None, since: date, offset: int) -> dict:
    payload = {
        "token": "", "pn": offset, "rn": ROWS, "sdt": "", "edt": "", "wd": "", "inc_wd": "",
        "exc_wd": "", "fields": "", "cnum": "001",
        "sort": '{"webdate":"0","id":"0"}',  # 按发布时间倒序
        "ssort": "", "cl": 500, "terminal": "",
        # 栏目 002 = 招标采购 002001 + 非招标采购 002002（前缀匹配）
        "condition": [_cond("categorynum", "002", like_type=2)],
        # 标题包含任一关键词
        "unionCondition": [_cond("searchtitle", k) for k in keywords] if keywords else None,
        "time": [{"fieldName": "webdate",
                  "startTime": since.strftime("%Y-%m-%d 00:00:00"),
                  "endTime": date.today().strftime("%Y-%m-%d 23:59:59")}],
        "highlights": "", "statistics": None, "accuracy": "", "noParticiple": "1",
        "searchRange": None, "noWd": True,
    }
    resp = session.post(API, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), headers={
        "Content-Type": "application/json;charset=UTF-8",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": BASE + "/jyxx/bid_purchase.html",
    })
    resp.raise_for_status()
    outer = resp.json()
    if outer.get("code") != 200:
        raise RuntimeError(f"国企采购接口返回 code={outer.get('code')!r}：{outer.get('msg')!r}")
    return json.loads(outer["content"])["result"]


def _notice(rec: dict) -> Notice:
    if rec.get("categorynum") == _INTENT_CATEGORY:
        notice_type, kind = "采购意向", "intent"
    else:
        notice_type, kind = _STAGES.get(rec.get("projecthuanjie") or "", (None, "other"))
    title = re.sub(r"<[^>]+>", "", rec.get("customtitle") or rec.get("title") or "").strip()
    purchaser = _PURCHASER_RE.search(rec.get("content") or "")
    return Notice(
        source="shggzy",
        source_id=rec["id"],
        title=title,
        url=BASE + rec["linkurl"],
        publish_date=(rec.get("webdate") or "")[:16],
        purchaser=purchaser.group(1) if purchaser else None,
        notice_type=notice_type,
        region=rec.get("xiaquname") or None,
        kind=kind,
        # 招标/采购公告的 gonggaotodate 就是投标截止时间；公示类是公示期结束时间，不显示
        extra={"deadline": (rec.get("gonggaotodate") or "")[:16] or None} if kind == "tender" else {},
    )


def fetch(session, cfg: Config, since: date) -> list[Notice]:
    notices: list[Notice] = []
    for page in range(MAX_PAGES):
        # 全量存档时不按标题关键词筛，保险筛选交给 filters.match
        result = _query(session, None if cfg.archive else cfg.keywords, since, page * ROWS)
        records = result.get("records") or []
        notices += [_notice(r) for r in records]
        if len(records) < ROWS or len(notices) >= int(result.get("totalcount") or 0):
            return notices
    log.warning("结果超过 %d 页，只取了前 %d 条", MAX_PAGES, len(notices))
    return notices
