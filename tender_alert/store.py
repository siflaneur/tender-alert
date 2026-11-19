import contextlib
import json
import re
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

from .models import Notice

SCHEMA = """
CREATE TABLE IF NOT EXISTS notices (
    source       TEXT NOT NULL,
    source_id    TEXT NOT NULL,
    title        TEXT NOT NULL,
    norm_title   TEXT NOT NULL,
    url          TEXT NOT NULL,
    publish_date TEXT NOT NULL,
    purchaser    TEXT,
    notice_type  TEXT,
    kind         TEXT NOT NULL,
    keywords     TEXT NOT NULL,
    extra        TEXT NOT NULL DEFAULT '{}',
    first_seen   TEXT NOT NULL,
    notified_at  TEXT,
    duplicate_of TEXT,
    PRIMARY KEY (source, source_id)
);
CREATE INDEX IF NOT EXISTS idx_norm_title ON notices(norm_title);
CREATE TABLE IF NOT EXISTS source_health (
    source               TEXT PRIMARY KEY,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    alerted              INTEGER NOT NULL DEFAULT 0,
    last_error           TEXT,
    last_success         TEXT
);
CREATE TABLE IF NOT EXISTS archive (
    source       TEXT NOT NULL,
    source_id    TEXT NOT NULL,
    title        TEXT NOT NULL,
    url          TEXT NOT NULL,
    publish_date TEXT NOT NULL,
    purchaser    TEXT,
    notice_type  TEXT,
    kind         TEXT NOT NULL,
    region       TEXT,
    extra        TEXT NOT NULL DEFAULT '{}',
    first_seen   TEXT NOT NULL,
    last_seen    TEXT NOT NULL,
    PRIMARY KEY (source, source_id)
);
CREATE INDEX IF NOT EXISTS idx_archive_date ON archive(publish_date);
CREATE INDEX IF NOT EXISTS idx_archive_last_seen ON archive(last_seen);
CREATE TABLE IF NOT EXISTS source_state (
    source TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);
"""

# 不同网站转载同一条公告时，同一标题的发布日期最多相差几天仍视为同一条
DUP_DAYS = 3


# 标题末尾的公告类型字样。各网站转载时常改写这部分，比如申通地铁的「…项目招标公告」
# 在综合采购上是「…项目采购公告」，「中标结果公告」变成「成交结果公告」，所以比较时去掉、只比项目名称。
# 「重新」「第二次」不在里面：重新招标是新的一轮，应该再提醒。
# 终止、废标、流标、失败、候选人也不在里面：它们和中标结果同属 result，但不是同一条公告。
_TYPE_SUFFIX = re.compile(
    r"(?:公告|公示|招标|采购|项目|中标|成交|结果|竞价|核价|询价|比选|谈判|磋商|单一来源|"
    r"更正|变更|澄清|意向|邀请|资格预审|文件)+$"
)
MIN_CORE = 6  # 去掉后缀后太短的（如「采购公告」本身）就用完整标题，免得误合并


def normalize_title(title: str) -> str:
    """去掉空白、标点和末尾的公告类型字样，用于跨来源判断是不是同一条公告。

    只比项目名称会把同一项目的招标公告和结果公告算成同名，所以 add 里还要求两条的 kind 相同。
    """
    full = _strip(title)
    core = _TYPE_SUFFIX.sub("", full)
    return core if len(core) >= MIN_CORE else full


def _strip(title: str) -> str:
    return re.sub(r"[\s\W_]+", "", title)


def _days_apart(a: str, b: str) -> int:
    try:
        return abs((date.fromisoformat(a[:10]) - date.fromisoformat(b[:10])).days)
    except ValueError:
        return 0 if a[:10] == b[:10] else DUP_DAYS + 1


class Store:
    """SQLite 存储：记住见过的公告（去重）和各来源的健康状况。

    scratch=True 时把数据库复制到内存里用，所有改动都不写回文件（--dry-run 用）。
    """

    def __init__(self, path: Path, scratch: bool = False):
        self._in_batch = False
        if scratch:
            self.db = sqlite3.connect(":memory:")
            self.db.row_factory = sqlite3.Row
            self.db.executescript(SCHEMA)
            if path.exists():
                self._copy_working_set(path)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            self.db = sqlite3.connect(path)
            self.db.row_factory = sqlite3.Row
            self.db.executescript(SCHEMA)
        self._renormalize()

    def _copy_working_set(self, path: Path) -> None:
        """--dry-run 内存库只复制小表及近 90 天/含保险字样的存档行，避免存档多年膨胀后整库拷贝变慢。"""
        cutoff = (date.today() - timedelta(days=90)).isoformat()
        self.db.execute("ATTACH DATABASE ? AS disk", (str(path),))
        try:
            for tbl in ("notices", "source_health", "source_state"):
                if self.db.execute("SELECT 1 FROM disk.sqlite_master WHERE type='table' AND name=?", (tbl,)).fetchone():
                    self.db.execute(f"INSERT INTO main.{tbl} SELECT * FROM disk.{tbl}")
            if self.db.execute("SELECT 1 FROM disk.sqlite_master WHERE type='table' AND name='archive'").fetchone():
                self.db.execute(
                    "INSERT INTO main.archive SELECT * FROM disk.archive"
                    " WHERE publish_date >= ? OR first_seen >= ?"
                    " OR title LIKE '%险%' OR title LIKE '%医疗%' OR title LIKE '%保障%' OR title LIKE '%承保%'",
                    (cutoff, cutoff),
                )
            self.db.commit()
        finally:
            self.db.execute("DETACH DATABASE disk")

    @contextlib.contextmanager
    def batch(self):
        """批量事务：把一个来源的一整批入库/存档合并成一次磁盘刷盘。"""
        prev = self._in_batch
        self._in_batch = True
        try:
            yield
            if not prev:
                self.db.commit()
        except Exception:
            if not prev:
                self.db.rollback()
            raise
        finally:
            self._in_batch = prev

    def _commit(self) -> None:
        if not self._in_batch:
            self.db.commit()

    def _renormalize(self) -> None:
        """去重规则改过后，把已有记录的 norm_title 按新规则重算（表很小，每次启动检查一遍）。"""
        rows = self.db.execute("SELECT source, source_id, title, norm_title FROM notices").fetchall()
        changed = [(normalize_title(r["title"]), r["source"], r["source_id"])
                   for r in rows if normalize_title(r["title"]) != r["norm_title"]]
        if changed:
            self.db.executemany("UPDATE notices SET norm_title = ? WHERE source = ? AND source_id = ?", changed)
            self.db.commit()

    def add(self, notice: Notice, keywords: list[str], priority: list[str] = ()) -> bool:
        """记录一条命中的公告，已存在返回 False。

        如果已有同一条（项目名称相同、公告类型相同、日期相近），只保留一条主记录用来提醒，其余标为重复。
        只放宽到不同网站之间：同一网站内、或类型有一边归不出来（other）时，退回老规则，完整标题（去掉空白标点）相同才算。
        同一网站内也会这样合并：东航平台上同一个项目常有几条 id 不同、标题日期完全一样的记录。
        主记录取 priority 里排在前面的来源；已经提醒过的那条始终保持为主记录，避免重复提醒。
        """
        exists = self.db.execute(
            "SELECT 1 FROM notices WHERE source = ? AND source_id = ?",
            (notice.source, notice.source_id),
        ).fetchone()
        if exists:
            return False

        def rank(source: str) -> int:
            return priority.index(source) if source in priority else len(priority)

        norm = normalize_title(notice.title)
        duplicate_of = None
        replaced_primary = False
        for row in self.db.execute(
            "SELECT source, source_id, title, publish_date, kind, notified_at FROM notices "
            "WHERE norm_title = ? AND duplicate_of IS NULL",
            (norm,),
        ).fetchall():
            if _days_apart(row["publish_date"], notice.publish_date) > DUP_DAYS:
                continue
            if row["source"] == notice.source or "other" in (row["kind"], notice.kind):
                # 同一网站自己改了后缀就是另一条（宝武「X」候选人公示和「X中标结果公告」、政府采购网合同公告和履约验收）；
                # 类型不明的也没法确认（东航「X」和「X（终止）」）：这两种都只认完整标题相同
                if _strip(row["title"]) != _strip(notice.title):
                    continue
            elif row["kind"] != notice.kind:
                continue  # 同一项目的招标公告和结果公告不是同一条
            if row["notified_at"] is None and rank(notice.source) < rank(row["source"]):
                self.db.execute(
                    "UPDATE notices SET duplicate_of = ? WHERE source = ? AND source_id = ?",
                    (f"{notice.source}:{notice.source_id}", row["source"], row["source_id"]),
                )
                replaced_primary = True
            else:
                duplicate_of = f"{row['source']}:{row['source_id']}"
            break
        self.db.execute(
            "INSERT INTO notices (source, source_id, title, norm_title, url, publish_date, purchaser,"
            " notice_type, kind, keywords, extra, first_seen, duplicate_of)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                notice.source, notice.source_id, notice.title, norm, notice.url,
                notice.publish_date, notice.purchaser, notice.notice_type, notice.kind,
                ",".join(keywords), json.dumps(notice.extra, ensure_ascii=False),
                datetime.now().isoformat(timespec="seconds"), duplicate_of,
            ),
        )
        self._commit()
        return duplicate_of is None and not replaced_primary

    def pending(self, since: str) -> list[sqlite3.Row]:
        """还没提醒过的公告（不含重复的），按发布日期从新到旧。

        发布日期解析不出来的也算在内（宁可多提醒也别漏），排在最后。
        """
        return self.db.execute(
            "SELECT * FROM notices WHERE notified_at IS NULL AND duplicate_of IS NULL"
            " AND (publish_date >= ? OR publish_date NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*')"
            " ORDER BY publish_date DESC, source",
            (since,),
        ).fetchall()

    def mark_notified(self, rows: list[sqlite3.Row]) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        self.db.executemany(
            "UPDATE notices SET notified_at = ? WHERE source = ? AND source_id = ?",
            [(now, r["source"], r["source_id"]) for r in rows],
        )
        self._commit()

    def update_extra(self, source: str, source_id: str, extra: dict) -> None:
        self.db.execute(
            "UPDATE notices SET extra = ? WHERE source = ? AND source_id = ?",
            (json.dumps(extra, ensure_ascii=False), source, source_id),
        )
        self._commit()

    def history_insurance_rows(self, since_date: str, until_date: str) -> list[sqlite3.Row]:
        """本地库（archive + notices）里在指定日期范围内的保险相关历史记录，供往年续标比对。"""
        sql = (
            "SELECT title, url, publish_date, purchaser, notice_type, kind FROM archive"
            " WHERE publish_date >= ? AND publish_date <= ?"
            " AND (title LIKE '%险%' OR title LIKE '%医疗%' OR title LIKE '%保障%' OR title LIKE '%承保%')"
            " UNION ALL "
            "SELECT title, url, publish_date, purchaser, notice_type, kind FROM notices"
            " WHERE duplicate_of IS NULL AND publish_date >= ? AND publish_date <= ?"
        )
        return self.db.execute(sql, (since_date, until_date + "z", since_date, until_date + "z")).fetchall()

    def record_success(self, source: str) -> None:
        self.db.execute(
            "INSERT INTO source_health (source, consecutive_failures, last_success) VALUES (?, 0, ?)"
            " ON CONFLICT(source) DO UPDATE SET consecutive_failures = 0, alerted = 0,"
            " last_success = excluded.last_success",
            (source, datetime.now().isoformat(timespec="seconds")),
        )
        self._commit()

    def record_failure(self, source: str, error: str) -> int:
        """记录一次失败，返回当前连续失败次数。"""
        self.db.execute(
            "INSERT INTO source_health (source, consecutive_failures, last_error) VALUES (?, 1, ?)"
            " ON CONFLICT(source) DO UPDATE SET consecutive_failures = consecutive_failures + 1,"
            " last_error = excluded.last_error",
            (source, error[:500]),
        )
        self._commit()
        return self.db.execute(
            "SELECT consecutive_failures FROM source_health WHERE source = ?", (source,)
        ).fetchone()[0]

    def last_success_date(self, source: str) -> date | None:
        row = self.db.execute("SELECT last_success FROM source_health WHERE source = ?", (source,)).fetchone()
        return date.fromisoformat(row["last_success"][:10]) if row and row["last_success"] else None

    def unalerted_failures(self, threshold: int) -> list[sqlite3.Row]:
        """连续失败达到阈值、但还没成功发出过告警邮件的来源。"""
        return self.db.execute(
            "SELECT source, consecutive_failures, last_error FROM source_health"
            " WHERE consecutive_failures >= ? AND alerted = 0",
            (threshold,),
        ).fetchall()

    def mark_alerted(self, sources: list[str]) -> None:
        self.db.executemany("UPDATE source_health SET alerted = 1 WHERE source = ?", [(s,) for s in sources])
        self._commit()

    def archive(self, notice: Notice) -> bool:
        """全量存档一条公告（不限保险），新存的返回 True；已有记录仅在内容真的变化时才更新并刷新 last_seen。"""
        extra_json = json.dumps(notice.extra, ensure_ascii=False)
        existing = self.db.execute(
            "SELECT title, url, publish_date, purchaser, notice_type, kind, region, extra"
            " FROM archive WHERE source = ? AND source_id = ?",
            (notice.source, notice.source_id),
        ).fetchone()
        now = datetime.now().isoformat(timespec="seconds")
        if existing is None:
            self.db.execute(
                "INSERT INTO archive (source, source_id, title, url, publish_date, purchaser, notice_type, kind,"
                " region, extra, first_seen, last_seen) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    notice.source, notice.source_id, notice.title, notice.url, notice.publish_date,
                    notice.purchaser, notice.notice_type, notice.kind, notice.region,
                    extra_json, now, now,
                ),
            )
            self._commit()
            return True
        new_fields = (
            notice.title, notice.url, notice.publish_date, notice.purchaser,
            notice.notice_type, notice.kind, notice.region, extra_json,
        )
        if tuple(existing) != new_fields:
            self.db.execute(
                "UPDATE archive SET title = ?, url = ?, publish_date = ?, purchaser = ?,"
                " notice_type = ?, kind = ?, region = ?, extra = ?, last_seen = ?"
                " WHERE source = ? AND source_id = ?",
                (*new_fields, now, notice.source, notice.source_id),
            )
            self._commit()
        return False

    def get_state(self, source: str) -> dict:
        """来源自己要记住的东西，比如上次翻到列表的哪一条。"""
        row = self.db.execute("SELECT value FROM source_state WHERE source = ?", (source,)).fetchone()
        return json.loads(row["value"]) if row else {}

    def set_state(self, source: str, state: dict) -> None:
        self.db.execute(
            "INSERT INTO source_state (source, value) VALUES (?, ?)"
            " ON CONFLICT(source) DO UPDATE SET value = excluded.value",
            (source, json.dumps(state, ensure_ascii=False)),
        )
        self._commit()
