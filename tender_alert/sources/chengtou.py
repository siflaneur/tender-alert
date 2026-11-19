"""上海城投集团 · 招采服务平台（zhaocai.chengtoucct.com）。

公告列表公开、无需登录，数据走 JSON 接口
`POST /fcsrm/fcb/bid/open/notice/supplier/queryPage`：接口不支持搜索，但全量只有几百条，
按 size/current 分页读取，翻到回看窗口之前就停（每次运行大约 1–2 页）。
字段有标题、招标人、发布时间、投标截止时间、预算、招标类型（公开招标/询价比选）。

短板：没有独立的公告详情页地址（点卡片是弹窗），邮件里的链接只能指向公告列表页。
公告量少（近 3 个月 40 多条，近 30 天保险类 0 条），属于低频补充来源。
"""

import json
import logging
from datetime import date

from ..config import Config
from ..models import Notice

NAME = "上海城投集团·招采服务平台"
REGION_SCOPED = True  # 城投集团及下属单位都在上海

log = logging.getLogger(__name__)

BASE = "https://zhaocai.chengtoucct.com"
LIST_URL = BASE + "/fcb/fcsrm/announcement/announcement.html"
API = BASE + "/fcsrm/fcb/bid/open/notice/supplier/queryPage"
PAGE_SIZE = 100
MAX_PAGES = 5


def _budget(raw) -> str | None:
    """预算金额（元）→「76万元」。"""
    if raw in (None, ""):
        return None
    try:
        yuan = float(raw)
    except (TypeError, ValueError):
        return str(raw)
    return f"{yuan / 10000:g}万元" if yuan >= 10000 else f"{yuan:g}元"


def _post(session, current: int) -> tuple[list[dict], int]:
    payload = {
        "data": {"bidTypeList": ["1", "4"], "qybh": "SHCT", "expiredCheckFlag": "0"},
        "size": PAGE_SIZE,
        "current": current,
    }
    resp = session.post(
        API,
        data=json.dumps(payload, ensure_ascii=False),
        headers={
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json;charset=UTF-8",
            "Origin": BASE,
            "Referer": LIST_URL,
        },
    )
    resp.raise_for_status()
    try:
        body = resp.json()
        result = body["result"]
        records = result.get("records") or []
        total = int(result.get("total") or 0)
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise RuntimeError("城投招采接口返回格式变了") from exc
    if str(body.get("status")) != "200":
        raise RuntimeError(f"城投招采接口返回异常：{body.get('message')!r}")
    return records, total


def _notice(rec: dict) -> Notice:
    return Notice(
        source="chengtou",
        source_id=str(rec.get("noticeId") or rec.get("noticeNo") or ""),
        title=(rec.get("noticeTitle") or rec.get("bidTitle") or "").strip(),
        url=LIST_URL,  # 没有独立详情页，链接到公告列表页
        publish_date=(rec.get("publishTime") or "")[:10],
        purchaser=rec.get("bidProjectCompanyName") or None,
        notice_type=rec.get("bidTypeName") or None,
        region="上海",
        kind="other",  # 让主流程按标题/公告类型归类（「公开招标」「询价比选」都会归为 tender）
        extra={
            "budget": _budget(rec.get("budgetAmount")),
            "deadline": (rec.get("bidEndTime") or "")[:16] or None,
        },
    )


def fetch(session, cfg: Config, since: date) -> list[Notice]:
    found: dict[str, Notice] = {}
    rows = 0
    for page in range(1, MAX_PAGES + 1):
        batch, total = _post(session, page)
        rows += len(batch)
        for rec in batch:
            notice = _notice(rec)
            if not notice.source_id or not notice.title or not notice.publish_date:
                continue
            found.setdefault(notice.source_id, notice)
        if not batch:
            break
        oldest = min((r.get("publishTime") or "")[:10] for r in batch if r.get("publishTime"))
        if len(batch) < PAGE_SIZE or rows >= total or oldest < since.isoformat():
            break
    else:
        log.warning("城投招采公告超过 %d 页，后面的公告没有抓取", MAX_PAGES)
    return list(found.values())
