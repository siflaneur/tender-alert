"""中远海运 · 集团采购信息系统（procurement.coscoshipping.com）。

门户的公开公告接口按公告创建时间分页返回完整正文，不需要打开详情页。
上海地区的公告全部返回（全量存档要用），保险筛选由主流程的 filters.match 做。
平台覆盖中远海运全国各单位，接口自带省市字段；这里只保留地区为上海的公告，
避免正文里出现上海供应商、收货地址等字样造成误判。网站没有 robots.txt。
"""

import json
import logging
import re
from datetime import date
from urllib.parse import quote

from bs4 import BeautifulSoup

from ..config import Config
from ..models import Notice

NAME = "中远海运·集团采购信息系统"
REGION_SCOPED = True

BASE = "https://procurement.coscoshipping.com"
LIST_URL = BASE + "/api/biz-portal-manage/biz/anno/getPage"
PORTAL_URL = BASE + "/portal/index"
ANNOUNCE_URL = BASE + "/portal/announce/announceInfo?id={id}"
PAGE_SIZE = 100
MAX_PAGES = 20

log = logging.getLogger(__name__)

_DATE_RE = re.compile(r"^20\d{2}-\d{2}-\d{2}$")
_PURCHASER_RE = re.compile(
    r"(?:项目)?(?:招标人|采购人)(?:为|是|[:：])\s*([^\s。；;，,]{2,80})"
)


def _post(session, page: int, since: date, today: date) -> tuple[int, list[dict]]:
    payload = {
        "basePageRequest": {"pageNo": page, "pageSize": PAGE_SIZE},
        "startTime": f"{since.isoformat()} 00:00:00",
        "endTime": f"{today.isoformat()} 23:59:59",
        "regionDictType": "1",
    }
    resp = session.post(
        LIST_URL,
        json=payload,
        headers={
            "Referer": PORTAL_URL,
            "Accept": "application/json, text/plain, */*",
        },
    )
    resp.raise_for_status()
    resp.encoding = "utf-8"
    try:
        body = json.loads(resp.text)
        data = body["data"]
        total = int(data["total"])
        records = data["records"]
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("中远海运公告接口返回格式变了") from exc
    if str(body.get("code")) != "200" or not isinstance(records, list):
        raise RuntimeError(f"中远海运公告接口返回异常：{body.get('message') or body.get('code')}")
    if total > 0 and not records:
        raise RuntimeError("中远海运公告接口有结果数但一条公告也没返回")
    return total, records


def _rows(session, since: date, today: date) -> list[dict]:
    rows: list[dict] = []
    total = 0
    for page in range(1, MAX_PAGES + 1):
        total, batch = _post(session, page, since, today)
        rows.extend(batch)
        if len(rows) >= total:
            break
        if not batch:
            raise RuntimeError("中远海运公告分页提前返回空列表")
    else:
        if len(rows) < total:
            log.warning(
                "中远海运公告超过 %d 页，后面的公告没有抓取",
                MAX_PAGES,
            )
    return rows


def _region(row: dict) -> str | None:
    region = (row.get("region") or "").strip()
    province = str(row.get("province") or "")
    region_id = str(row.get("regionId") or "")
    region_en = (row.get("regionEn") or "").strip()
    if (
        region == "上海市"
        or province.startswith("31")
        or region_id == "310000"
        or region_en.lower() == "shanghai"
    ):
        return "上海"
    return region or None


def _purchaser(row: dict) -> str | None:
    text = BeautifulSoup(row.get("annoText") or "", "lxml").get_text(" ", strip=True)
    match = _PURCHASER_RE.search(text)
    return match.group(1).strip(" ：:，,") if match else None


def _published(row: dict) -> str | None:
    value = (row.get("createTime") or row.get("annoStartDate") or "")[:10]
    return value if _DATE_RE.match(value) else None


def fetch(session, cfg: Config, since: date) -> list[Notice]:
    today = date.today()
    notices = []
    for row in _rows(session, since, today):
        title = (row.get("annoName") or "").strip()
        if not title:  # 不在这里按关键词筛：全量存档要所有上海公告，保险筛选交给主流程
            continue
        if _region(row) != "上海":
            continue
        published = _published(row)
        source_id = str(row.get("id") or "")
        if not published or not source_id:
            continue
        notices.append(Notice(
            source="cosco",
            source_id=source_id,
            title=title,
            url=ANNOUNCE_URL.format(id=quote(source_id, safe="")),
            publish_date=published,
            purchaser=_purchaser(row),
            notice_type=(row.get("pAnnoType") or row.get("annoType")),
            region="上海",
        ))
    return notices
