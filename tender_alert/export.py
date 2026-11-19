"""把数据库里的公告导出成 CSV（Excel 直接打开）或 JSON（给程序读）。

- 保险命中的历史（notices 表）：每次正式运行后更新一个 CSV；JSON 用 --export-json 手动导出。
  只导出主记录，其他网站转载的重复公告不重复列出。
- 全量存档（archive 表）：数据量大，按发布月份拆成多个 CSV，每次运行只重写这次有变动的月份；
  --export-archive 重新导出全部月份。
"""

import csv
import json
import logging
import os
import sqlite3
from pathlib import Path

from .mailer import KIND_LABELS
from .prior import format_prior_text
from .sources import source_display_name

log = logging.getLogger(__name__)

# (列名, 取值函数)；顺序就是 CSV 的列顺序
COLUMNS = [
    ("发布日期", lambda r, x, n: r["publish_date"]),
    ("类型", lambda r, x, n: KIND_LABELS.get(r["kind"], r["kind"])),
    ("标题", lambda r, x, n: r["title"]),
    ("采购单位", lambda r, x, n: r["purchaser"] or ""),
    ("预算", lambda r, x, n: x.get("budget") or ""),
    ("截止/开标", lambda r, x, n: x.get("deadline") or ""),
    ("命中关键词", lambda r, x, n: r["keywords"]),
    ("往年及原中标", lambda r, x, n: format_prior_text(x.get("prior"))),
    ("来源", lambda r, x, n: n),
    ("首次发现", lambda r, x, n: r["first_seen"].replace("T", " ")),
    ("提醒时间", lambda r, x, n: (r["notified_at"] or "未提醒").replace("T", " ")),
    ("链接", lambda r, x, n: r["url"]),
]


def _source_name(key: str, cache: dict[str, str]) -> str:
    if key not in cache:
        cache[key] = source_display_name(key)
    return cache[key]


def load_rows(db_path: Path) -> list[dict]:
    """读出所有主记录（不含重复转载），按发布日期从新到旧，每条是 列名 → 值。"""
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)  # 只读打开，不影响正在运行的程序
    db.row_factory = sqlite3.Row
    try:
        records = db.execute(
            "SELECT * FROM notices WHERE duplicate_of IS NULL ORDER BY publish_date DESC, first_seen DESC"
        ).fetchall()
    finally:
        db.close()
    names: dict[str, str] = {}
    rows = []
    for r in records:
        extra = json.loads(r["extra"] or "{}")
        source = _source_name(r["source"], names)
        rows.append({col: get(r, extra, source) for col, get in COLUMNS})
    return rows


def _write_atomically(path: Path, write) -> None:
    """先写临时文件再替换，导出到一半出错也不会留下残缺文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    write(tmp)
    os.replace(tmp, path)


def export_csv(db_path: Path, out_path: Path) -> int:
    """导出 CSV，返回条数。用 UTF-8 BOM，Excel 打开中文不会乱码。"""
    rows = load_rows(db_path)

    def write(tmp: Path) -> None:
        with open(tmp, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=[c for c, _ in COLUMNS])
            w.writeheader()
            w.writerows(rows)

    _write_atomically(out_path, write)
    return len(rows)


def export_json(db_path: Path, out_path: Path) -> int:
    rows = load_rows(db_path)
    _write_atomically(out_path, lambda tmp: tmp.write_text(
        json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8"))
    return len(rows)


# 全量存档 CSV 的列
ARCHIVE_COLUMNS = [
    ("发布日期", lambda r, x, n: r["publish_date"]),
    ("类型", lambda r, x, n: KIND_LABELS.get(r["kind"], r["kind"])),
    ("公告类型", lambda r, x, n: r["notice_type"] or ""),
    ("标题", lambda r, x, n: r["title"]),
    ("采购单位", lambda r, x, n: r["purchaser"] or ""),
    ("预算", lambda r, x, n: x.get("budget") or ""),
    ("截止/开标", lambda r, x, n: x.get("deadline") or ""),
    ("来源", lambda r, x, n: n),
    ("首次发现", lambda r, x, n: r["first_seen"].replace("T", " ")),
    ("链接", lambda r, x, n: r["url"]),
]

# 发布日期解析不出来的公告按首次发现的月份归档
_MONTH_SQL = (
    "CASE WHEN publish_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]*' "
    "THEN substr(publish_date, 1, 7) ELSE substr(first_seen, 1, 7) END"
)


def export_archive_csv(db_path: Path, out_dir: Path, touched_since: str | None = None) -> dict[str, int]:
    """按发布月份导出全量存档，返回 {月份: 条数}。

    touched_since（ISO 时间）给了就只重写这之后有新增或更新的月份，不给就重写全部月份。
    文件被 Excel 占用时跳过那个月，下次再更新。
    """
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'archive'").fetchone():
            return {}
        if touched_since:
            months = [r[0] for r in db.execute(
                f"SELECT DISTINCT {_MONTH_SQL} FROM archive WHERE last_seen >= ?", (touched_since,))]
        else:
            months = [r[0] for r in db.execute(f"SELECT DISTINCT {_MONTH_SQL} FROM archive")]
        names: dict[str, str] = {}
        written: dict[str, int] = {}
        for month in sorted(months):
            records = db.execute(
                f"SELECT * FROM archive WHERE {_MONTH_SQL} = ? ORDER BY publish_date DESC, first_seen DESC", (month,)
            ).fetchall()
            rows = [{col: get(r, json.loads(r["extra"] or "{}"), _source_name(r["source"], names))
                     for col, get in ARCHIVE_COLUMNS} for r in records]

            def write(tmp: Path, rows=rows) -> None:
                with open(tmp, "w", encoding="utf-8-sig", newline="") as f:
                    w = csv.DictWriter(f, fieldnames=[c for c, _ in ARCHIVE_COLUMNS])
                    w.writeheader()
                    w.writerows(rows)

            try:
                _write_atomically(out_dir / f"{month}.csv", write)
            except PermissionError:
                log.warning("%s.csv 正被别的程序（多半是 Excel）打开，没能更新，关掉后下次运行会更新", month)
                continue
            written[month] = len(rows)
        return written
    finally:
        db.close()
