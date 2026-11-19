import csv
import json
import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from tender_alert import __main__ as cli
from tender_alert.export import COLUMNS, export_archive_csv, export_csv, export_json
from tender_alert.models import Notice
from tender_alert.store import Store


def notice(title, source, sid, day, **extra):
    return Notice(source=source, source_id=sid, title=title, url=f"https://example.com/{sid}",
                  publish_date=day, purchaser="某单位", kind="tender", extra=extra)


class ExportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "t.db"
        store = Store(self.db)
        store.add(notice("某局车辆保险服务招标公告", "zfcg_sh", "a", "2026-10-20", budget="50万元"), ["保险"])
        store.add(notice("某局车辆保险服务招标公告", "ccgp", "b", "2026-10-21"), ["保险"])  # 转载，算重复
        store.add(notice("某医院医疗责任保险采购公告", "shggzy_zhcg", "c", "2026-10-28"), ["保险", "责任险"])
        store.mark_notified([r for r in store.pending("2026-10-01") if r["source_id"] == "a"])
        store.db.close()

    def tearDown(self):
        self.tmp.cleanup()

    def test_csv_lists_primary_records_newest_first(self):
        out = Path(self.tmp.name) / "exports" / "历史.csv"
        self.assertEqual(export_csv(self.db, out), 2)
        raw = out.read_bytes()
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))  # 带 BOM，Excel 打开中文不乱码
        with open(out, encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(list(rows[0].keys()), [c for c, _ in COLUMNS])
        self.assertEqual([r["标题"] for r in rows], ["某医院医疗责任保险采购公告", "某局车辆保险服务招标公告"])
        self.assertEqual(rows[0]["提醒时间"], "未提醒")
        self.assertEqual(rows[0]["来源"], "上海公共资源交易平台·综合采购")
        self.assertEqual(rows[1]["预算"], "50万元")
        self.assertEqual(rows[1]["类型"], "招标/采购")
        self.assertFalse(out.with_name(out.name + ".tmp").exists())

    def test_json_has_same_rows(self):
        out = Path(self.tmp.name) / "历史.json"
        self.assertEqual(export_json(self.db, out), 2)
        rows = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(rows[1]["命中关键词"], "保险")

    def test_csv_locked_by_excel_does_not_fail_the_run(self):
        with mock.patch.object(cli, "DB_PATH", self.db), \
                mock.patch.object(cli, "export_csv", side_effect=PermissionError("被占用")):
            cli.update_history_csv()  # 只记警告，不抛异常

    def test_no_database_yet_is_skipped(self):
        with mock.patch.object(cli, "DB_PATH", Path(self.tmp.name) / "none.db"), \
                mock.patch.object(cli, "export_csv") as export:
            cli.update_history_csv()
        export.assert_not_called()


class ArchiveExportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "t.db"
        self.out = Path(self.tmp.name) / "全量存档"
        store = Store(self.db)
        store.archive(notice("学校食堂食材采购公告", "zfcg_sh", "1", "2026-10-30 09:00", budget="10万元"))
        store.archive(notice("道路养护服务采购公告", "zfcg_sh", "2", "2026-11-02 09:00"))
        store.archive(notice("日期不明的公告", "srcb", "3", "未知"))
        store.db.close()

    def tearDown(self):
        self.tmp.cleanup()

    def test_splits_by_month(self):
        written = export_archive_csv(self.db, self.out)
        this_month = date.today().strftime("%Y-%m")  # 日期不明的按首次发现的月份归档
        expected = {"2026-10": 1, "2026-11": 1}
        expected[this_month] = expected.get(this_month, 0) + 1
        self.assertEqual(written, expected)
        with open(self.out / "2026-10.csv", encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(rows[0]["标题"], "学校食堂食材采购公告")
        self.assertEqual(rows[0]["预算"], "10万元")
        self.assertEqual(rows[0]["来源"], "上海政府采购网")

    def test_only_touched_months_are_rewritten(self):
        self.assertEqual(export_archive_csv(self.db, self.out, touched_since="2999-01-01T00:00:00"), {})
        self.assertFalse(self.out.exists())

    def test_missing_archive_table(self):
        empty = Path(self.tmp.name) / "empty.db"
        sqlite3.connect(empty).close()
        self.assertEqual(export_archive_csv(empty, self.out), {})


if __name__ == "__main__":
    unittest.main()
