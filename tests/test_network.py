import unittest
from unittest import mock

import requests

from tender_alert import http


class WaitForNetworkTest(unittest.TestCase):
    def test_returns_once_network_comes_up(self):
        calls = [requests.ConnectionError("还没联网"), requests.ConnectionError("还没联网"), None]
        with mock.patch.object(http.requests, "head", side_effect=calls) as head, \
                mock.patch.object(http.time, "sleep") as sleep:
            self.assertTrue(http.wait_for_network(timeout=180, interval=15))
        self.assertEqual(head.call_count, 3)
        self.assertEqual(sleep.call_count, 2)

    def test_gives_up_after_timeout(self):
        clock = iter(range(0, 1000, 15))  # 每次取时间都过去 15 秒
        with mock.patch.object(http.requests, "head", side_effect=requests.ConnectionError("断网")), \
                mock.patch.object(http.time, "sleep"), \
                mock.patch.object(http.time, "monotonic", side_effect=lambda: next(clock)):
            self.assertFalse(http.wait_for_network(timeout=60, interval=15))


def response(status, **headers):
    r = requests.Response()
    r.status_code = status
    r.headers.update(headers)
    return r


class PoliteSessionThrottleTest(unittest.TestCase):
    def test_429_waits_retry_after_and_slows_down(self):
        s = http.PoliteSession(interval=2)
        with mock.patch.object(requests.Session, "request", side_effect=[response(429, **{"Retry-After": "7"}), response(200)]), \
                mock.patch.object(http.time, "sleep") as sleep:
            self.assertEqual(s.get("https://example.com/a").status_code, 200)
        self.assertIn(mock.call(7.0), sleep.call_args_list)  # 照 Retry-After 等
        self.assertEqual(s._slow["example.com"], 4)  # 间隔从 2 秒加倍到 4 秒

    def test_429_without_retry_after_backs_off(self):
        s = http.PoliteSession(interval=2)
        with mock.patch.object(requests.Session, "request", side_effect=[response(429), response(200)]), \
                mock.patch.object(http.time, "sleep") as sleep:
            s.get("https://example.com/a")
        self.assertIn(mock.call(10), sleep.call_args_list)

    def test_retry_after_too_long_gives_up(self):
        s = http.PoliteSession(interval=2)
        with mock.patch.object(requests.Session, "request", return_value=response(429, **{"Retry-After": "3600"})), \
                mock.patch.object(http.time, "sleep"):
            with self.assertRaises(requests.HTTPError):
                s.get("https://example.com/a")

    def test_slow_down_is_capped(self):
        s = http.PoliteSession(interval=40)
        s._slow_down("x")
        s._slow_down("x")
        self.assertEqual(s._slow["x"], http.MAX_INTERVAL)


if __name__ == "__main__":
    unittest.main()
