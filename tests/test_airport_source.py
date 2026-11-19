import json
import unittest
from datetime import date
from unittest import mock

from tender_alert.config import Config
from tender_alert.models import classify_kind
from tender_alert.sources import airport

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


def api_response(items, total=None):
    return json.dumps({
        "success": True,
        "status": 200,
        "data": {"pageIndex": 1, "totalItems": len(items) if total is None else total, "items": items},
    }, ensure_ascii=False)


def item(pid, title, day, deadline="2026-11-05 10:00:00"):
    return {"pid": pid, "typeID": "604", "title": title, "createDate": day,
            "subTitle1Date": "2026-10-22 16:00:00", "subTitle2Date": deadline}


class FakeResponse:
    def __init__(self, text=""):
        self.text = text
        self.encoding = None

    def raise_for_status(self):
        pass

    def json(self):
        return json.loads(self.text)


class FakeSession:
    def __init__(self, pages_by_term=None):
        self.pages_by_term = pages_by_term or {}
        self.headers = {}
        self.get_calls = []
        self.post_calls = []

    def get(self, url, params=None, headers=None):
        self.get_calls.append(url)
        return FakeResponse("<html>列表页</html>")

    def post(self, url, data=None, headers=None):
        self.post_calls.append((url, data, headers))
        term = data["key"]
        page = int(data["pageIndex"])
        pages = self.pages_by_term.get(term, [])
        items = pages[page - 1] if page <= len(pages) else []
        return FakeResponse(api_response(items, total=sum(len(p) for p in pages)))


class AirportTest(unittest.TestCase):
    def test_opens_list_page_first_and_maps_fields(self):
        session = FakeSession({"险": [[item(
            19830, "上海浦东国际机场运行保障基地项目建安工程一切险", "2026-10-19")]]})

        notices = airport.fetch(session, CFG, SINCE)

        self.assertEqual(session.get_calls, [airport.LIST_URL])
        self.assertEqual([c[1]["key"] for c in session.post_calls], list(airport.SEARCH_TERMS))
        self.assertEqual(len(notices), 1)
        n = notices[0]
        self.assertEqual(n.source, "airport")
        self.assertEqual(n.source_id, "19830")
        self.assertEqual(n.title, "上海浦东国际机场运行保障基地项目建安工程一切险")
        self.assertEqual(n.url, airport.LIST_URL)
        self.assertEqual(n.publish_date, "2026-10-19")
        self.assertIsNone(n.notice_type)
        self.assertEqual(n.region, "上海")
        self.assertEqual(n.kind, "other")  # 由主流程按标题归类
        self.assertEqual(n.extra["deadline"], "2026-11-05 10:00")

    def test_kind_follows_title(self):
        session = FakeSession({"险": [[
            item(1, "2026年集团本部公务用车保险采购", "2026-10-19"),
            item(2, "2026年集团本部公务用车保险采购中标结果公示", "2026-10-25"),
        ]]})
        kinds = {n.source_id: classify_kind(n.notice_type, n.title) for n in airport.fetch(session, CFG, SINCE)}
        self.assertEqual(kinds, {"1": "other", "2": "result"})

    def test_same_pid_across_search_terms_is_one_notice(self):
        shared = item(1, "2026年集团本部公务用车保险采购", "2026-08-13")
        session = FakeSession({"险": [[shared]], "医疗": [[shared]], "保障": [[shared]]})

        notices = airport.fetch(session, CFG, SINCE)

        self.assertEqual([n.source_id for n in notices], ["1"])

    def test_paginates_until_total(self):
        with mock.patch.object(airport, "PAGE_SIZE", 2):
            first = [item(1, "A保险", "2026-10-01"), item(2, "B保险", "2026-10-02")]
            second = [item(3, "C保险", "2026-10-03")]
            session = FakeSession({"险": [first, second]})

            notices = airport.fetch(session, CFG, SINCE)

        self.assertEqual({n.source_id for n in notices}, {"1", "2", "3"})
        self.assertEqual(len([c for c in session.post_calls if c[1]["key"] == "险"]), 2)

    def test_bad_response_raises(self):
        session = FakeSession()
        session.post = lambda *args, **kwargs: FakeResponse("<html>网站改版了</html>")
        with self.assertRaises(RuntimeError):
            airport.fetch(session, CFG, SINCE)

    def test_api_failure_raises(self):
        session = FakeSession()
        session.post = lambda *args, **kwargs: FakeResponse(
            json.dumps({"success": False, "message": "参数错误"}))
        with self.assertRaises(RuntimeError):
            airport.fetch(session, CFG, SINCE)


if __name__ == "__main__":
    unittest.main()
