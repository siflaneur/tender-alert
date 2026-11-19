import tempfile
import unittest
from pathlib import Path
from unittest import mock

import requests

from tender_alert import http
from tender_alert.robots import RobotsDisallowed, RobotsGuard, allowed, parse

SHGGZY = """User-agent: *
Allow: /$
Disallow: /search/
Disallow: /jyxx*/
Crawl-delay: 1
"""


def response(status, text="", content_type="text/plain"):
    r = requests.Response()
    r.status_code = status
    r._content = text.encode("utf-8")
    r.headers["Content-Type"] = content_type
    r.encoding = "utf-8"
    return r


class ParseAndMatchTest(unittest.TestCase):
    def test_wildcards_and_longest_match(self):
        rules = parse(SHGGZY)
        self.assertEqual(allowed(rules, "/")[0], True)
        self.assertEqual(allowed(rules, "/search/queryContents.jhtml?title=x")[0], False)
        self.assertEqual(allowed(rules, "/jyxxwzcggg/9128837?cExt=abc")[0], False)  # 详情页被禁
        self.assertEqual(allowed(rules, "/jyxxwzcggg_2.jhtml?cExt=abc")[0], True)  # 列表页字面上不在禁止范围
        self.assertEqual(allowed(parse("User-agent: *\nDisallow: /a\nAllow: /a/b"), "/a/b/c"), (True, "/a/b"))
        self.assertEqual(allowed(parse("User-agent: *\nDisallow: /*.php$"), "/x.php?y=1")[0], True)

    def test_only_star_group_counts(self):
        rules = parse("User-agent: Baiduspider\nDisallow: /\n\nUser-agent: *\nDisallow: /private\n")
        self.assertEqual(rules, [(False, "/private")])
        self.assertEqual(parse("User-agent: *\nDisallow:\n"), [])  # 空 Disallow = 全部允许


class GuardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.guard = RobotsGuard(Path(self.tmp.name) / "robots.json")
        self.session = requests.Session()

    def tearDown(self):
        self.tmp.cleanup()

    def check(self, url, robots_resp):
        with mock.patch.object(requests.Session, "request", return_value=robots_resp) as req:
            self.guard.check(self.session, url)
        return req

    def test_disallowed_raises(self):
        with self.assertRaises(RobotsDisallowed):
            self.check("https://www.shggzy.com/jyxxwzcggg/1?cExt=x", response(200, SHGGZY))

    def test_authorized_exception_skips_check(self):
        req = self.check("https://www.shggzy.com/search/queryContents.jhtml?title=保险", response(200, SHGGZY))
        req.assert_not_called()

    def test_html_or_404_means_no_rules(self):
        self.check("https://a.example/anything", response(200, "<!DOCTYPE html><html>首页</html>", "text/html"))
        self.check("https://b.example/anything", response(404, "not found"))

    def test_server_error_allows_but_does_not_cache(self):
        self.check("https://c.example/x", response(503))
        self.assertNotIn("https://c.example", self.guard.cache)
        req = self.check("https://c.example/y", response(503))
        req.assert_not_called()  # 同一次运行里不再重复取

    def test_cache_is_reused(self):
        self.check("https://www.shggzy.com/jyxxwzcggg", response(200, SHGGZY))
        guard2 = RobotsGuard(self.guard.cache_path)
        with mock.patch.object(requests.Session, "request") as req:
            with self.assertRaises(RobotsDisallowed):
                guard2.check(self.session, "https://www.shggzy.com/jyxxwzcggg/1")
        req.assert_not_called()


class PoliteSessionRobotsTest(unittest.TestCase):
    def test_params_are_checked_and_blocked_request_is_not_sent(self):
        guard = mock.Mock()
        guard.check.side_effect = RobotsDisallowed("禁止")
        s = http.PoliteSession(interval=0, robots=guard)
        with mock.patch.object(requests.Session, "request") as req:
            with self.assertRaises(RobotsDisallowed):
                s.get("https://example.com/search", params={"q": "保险"})
        guard.check.assert_called_once()
        self.assertTrue(guard.check.call_args[0][1].endswith("/search?q=%E4%BF%9D%E9%99%A9"))
        req.assert_not_called()


if __name__ == "__main__":
    unittest.main()
