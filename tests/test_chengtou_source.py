import json
import unittest
from datetime import date
from unittest import mock

from tender_alert.config import Config
from tender_alert.sources import chengtou

CFG = Config(
    keywords=["保险", "责任险"],
    exclude=[],
    regions=["上海"],
    kinds=["tender", "correction", "other"],
    lookback_days=30,
    sources=[],
    request_interval=0,
    failure_threshold=3,
)
SINCE = date(2026, 9, 30)


def record(nid, title, day, buyer="制水公司", bid_type="公开招标",
           budget=760000, deadline="2026-11-11 09:00:00"):
    return {
        "noticeId": nid,
        "type": 1,
        "noticeTitle": title,
        "bidTitle": title,
        "bidProjectCompanyName": buyer,
        "bidTypeName": bid_type,
        "publishTime": f"{day} 16:00:00",
        "budgetAmount": budget,
        "bidEndTime": deadline,
        "noticeNo": f"ZBGG{nid}",
    }


class FakeResponse:
    def __init__(self, text=""):
        self.text = text
        self.encoding = None

    def raise_for_status(self):
        pass

    def json(self):
        return json.loads(self.text)


class FakeSession:
    def __init__(self, pages=None):
        self.pages = pages or []
        self.headers = {}
        self.post_calls = []

    def post(self, url, data=None, headers=None):
        payload = json.loads(data)
        current = payload["current"]
        self.post_calls.append((url, payload, headers))
        records = self.pages[current - 1] if current <= len(self.pages) else []
        body = {
            "status": "200",
            "message": "成功",
            "result": {"total": sum(len(p) for p in self.pages), "size": 100,
                       "current": current, "records": records},
        }
        return FakeResponse(json.dumps(body, ensure_ascii=False))


class ChengTouTest(unittest.TestCase):
    def test_maps_fields_and_links_to_list_page(self):
        session = FakeSession([[
            record("n1", "2027-2028年制水分公司振动数据监测系统维护服务项目", "2026-10-30"),
        ]])

        notices = chengtou.fetch(session, CFG, SINCE)

        self.assertEqual(len(notices), 1)
        n = notices[0]
        self.assertEqual(n.source, "chengtou")
        self.assertEqual(n.source_id, "n1")
        self.assertEqual(n.title, "2027-2028年制水分公司振动数据监测系统维护服务项目")
        self.assertEqual(n.url, chengtou.LIST_URL)
        self.assertEqual(n.publish_date, "2026-10-30")
        self.assertEqual(n.purchaser, "制水公司")
        self.assertEqual(n.notice_type, "公开招标")
        self.assertEqual(n.region, "上海")
        self.assertEqual(n.extra["budget"], "76万元")
        self.assertEqual(n.extra["deadline"], "2026-11-11 09:00")
        payload = session.post_calls[0][1]
        self.assertEqual(payload["data"]["bidTypeList"], ["1", "4"])
        self.assertEqual(payload["data"]["expiredCheckFlag"], "0")

    def test_stops_after_page_reaching_before_window(self):
        with mock.patch.object(chengtou, "PAGE_SIZE", 2):
            page1 = [record("a", "A项目", "2026-10-30"), record("b", "B项目", "2026-10-29")]
            page2 = [record("c", "C项目", "2026-09-20"), record("d", "D项目", "2026-09-19")]
            session = FakeSession([page1, page2])

            notices = chengtou.fetch(session, CFG, SINCE)

        self.assertEqual(len(session.post_calls), 2)
        self.assertEqual({n.source_id for n in notices}, {"a", "b", "c", "d"})

    def test_budget_is_optional(self):
        session = FakeSession([[record("n1", "某采购项目", "2026-10-30", budget=None)]])

        notices = chengtou.fetch(session, CFG, SINCE)

        self.assertIsNone(notices[0].extra["budget"])

    def test_bad_response_raises(self):
        session = FakeSession()
        session.post = lambda *args, **kwargs: FakeResponse("<html>网站改版了</html>")
        with self.assertRaises(RuntimeError):
            chengtou.fetch(session, CFG, SINCE)

    def test_api_failure_raises(self):
        session = FakeSession()
        session.post = lambda *args, **kwargs: FakeResponse(
            json.dumps({"status": "500", "message": "系统错误"}))
        with self.assertRaises(RuntimeError):
            chengtou.fetch(session, CFG, SINCE)


if __name__ == "__main__":
    unittest.main()
