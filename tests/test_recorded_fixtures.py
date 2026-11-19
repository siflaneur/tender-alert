"""用录下的真实响应回放每个来源，检查解析结果正常（样本在 tests/fixtures/，用 --dry-run --record 录制）。"""

import copy
import re
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from replay import ReplaySession
from tender_alert.config import Config, load_config
from tender_alert.recording import load
from tender_alert.sources import load_source

FIXTURES = Path(__file__).parent / "fixtures"


class RecordedFixtureTest(unittest.TestCase):
    def test_every_recorded_source_still_parses(self):
        files = sorted(FIXTURES.glob("*.json.gz"))
        if not files:
            self.skipTest("还没有录制样本：.\run.ps1 --dry-run --record")
        base = load_config()
        for path in files:
            fixture = load(path)
            with self.subTest(source=fixture["source"]):
                module = load_source(fixture["source"])
                cfg = Config(**{**base.__dict__, "archive": fixture["archive"]})
                session = ReplaySession(fixture)
                since = date.fromisoformat(fixture["since"])
                with mock.patch("time.sleep"):
                    if getattr(module, "STATEFUL", False):
                        notices = module.fetch(session, cfg, since, copy.deepcopy(fixture["state"]), dry_run=True)
                    else:
                        notices = module.fetch(session, cfg, since)
                self.assertEqual(session.pos, len(fixture["responses"]), "代码比录制时少发了请求")
                self.assertTrue(notices, "一条公告也没解析出来")
                for n in notices:
                    self.assertTrue(n.title.strip(), n)
                    self.assertTrue(n.url.startswith("http"), n)
                dated = [n for n in notices if re.match(r"20\d\d-\d\d-\d\d", n.publish_date)]
                self.assertGreaterEqual(len(dated), len(notices) * 0.9, "太多公告没有发布日期")


if __name__ == "__main__":
    unittest.main()
