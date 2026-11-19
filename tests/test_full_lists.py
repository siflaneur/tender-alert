"""全量存档模式下，各来源改抓全部公告列表的测试。"""

import unittest
from datetime import date, datetime, timedelta, timezone

from tender_alert.config import Config
import json

from unittest import mock

from tender_alert.sources import airport, baowu, ccgp, ceair, ebscn, gtht, shggzy, zfcg_sh

CFG = Config(keywords=["保险"], exclude=[], regions=["上海"], kinds=["tender", "correction", "other"],
             lookback_days=30, sources=[], request_interval=0, failure_threshold=3, archive=True)
CST = timezone(timedelta(hours=8))


class FakeResponse:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


def ms(day: str) -> int:
    return int(datetime.fromisoformat(day + " 10:00").replace(tzinfo=CST).timestamp() * 1000)


class ZfcgFullListTest(unittest.TestCase):
    def test_lists_every_category_until_since(self):
        calls = []

        def post(url, json=None, headers=None):
            calls.append((url, json))
            if not url.endswith("/portal/category"):
                raise AssertionError("存档模式不该再用关键词搜索")
            if json["categoryCode"] != "ZcyAnnouncement2":
                return FakeResponse({"success": True, "result": {"data": {"data": []}}})
            page = [{"articleId": f"{json['pageNo']}-{i}", "title": f"学校食堂食材采购公告{json['pageNo']}-{i}",
                     "publishDate": ms("2026-11-02" if json["pageNo"] == 1 else "2026-10-30"),
                     "pathName": "公开招标公告", "purchaseName": "某学校"} for i in range(zfcg_sh.LIST_PAGE_SIZE)]
            return FakeResponse({"success": True, "result": {"data": {"data": page}}})

        session = type("S", (), {"post": staticmethod(post)})()
        notices = zfcg_sh.fetch(session, CFG, date(2026, 11, 1))
        self.assertEqual(len(notices), zfcg_sh.LIST_PAGE_SIZE)  # 第 2 页早于 since，只留第 1 页
        self.assertEqual({c[1]["categoryCode"] for c in calls}, {code for code, _ in zfcg_sh.ALL_CATEGORIES})
        self.assertEqual(sum(1 for c in calls if c[1]["categoryCode"] == "ZcyAnnouncement2"), 2)
        self.assertTrue(notices[0].url.startswith("https://www.zfcg.sh.gov.cn/site/detail?categoryCode=ZcyAnnouncement&articleId="))



class KeywordFreeListsTest(unittest.TestCase):
    def test_gqcg_archive_mode_sends_no_keyword_condition(self):
        sent = []

        def post(url, data=None, headers=None):
            sent.append(json.loads(data))
            inner = {"result": {"totalcount": 1, "records": [
                {"id": "1", "title": "某国企食堂外包采购公告", "linkurl": "/jyxx/1.html", "webdate": "2026-11-02 09:00:00",
                 "categorynum": "002002001", "projecthuanjie": "007", "content": ""}]}}
            return FakeResponse({"code": 200, "content": json.dumps(inner)})

        session = type("S", (), {"post": staticmethod(post)})()
        notices = shggzy.fetch(session, CFG, date(2026, 11, 1))
        self.assertIsNone(sent[0]["unionCondition"])
        self.assertEqual([n.title for n in notices], ["某国企食堂外包采购公告"])

    def test_gtht_archive_mode_sends_no_title(self):
        sent = []

        def get(url, params=None, headers=None):
            sent.append(params)
            return FakeResponse({"code": "0", "data": {"total": 1, "list": [
                {"id": "x", "projectId": "p", "title": "国泰海通证券2026年交换机采购项目询价公告", "publishTime": "2026-11-02"}]}})

        session = type("S", (), {"get": staticmethod(get), "mount": lambda self, *a: None})()
        notices = gtht.fetch(session, CFG, date(2026, 11, 1))
        self.assertTrue(all("title" not in p for p in sent))
        self.assertEqual({p["noticeFlag"] for p in sent}, {"01", "02"})  # 存档要全部类型，含结果公告
        self.assertEqual(len(notices), 2)

    def test_airport_archive_mode_lists_all_until_since(self):
        posts = []

        def post(url, data=None, headers=None):
            posts.append(data)
            day = "2026-11-02" if data["pageIndex"] == 1 else "2026-10-20"
            items = [{"pid": f"{data['pageIndex']}-{i}", "title": f"航站楼维修项目{i}", "createDate": day}
                     for i in range(airport.PAGE_SIZE)]
            return FakeResponse({"success": True, "data": {"items": items, "totalItems": 5000}})

        session = type("S", (), {"post": staticmethod(post), "get": lambda self, *a, **k: None, "headers": {}})()
        notices = airport.fetch(session, CFG, date(2026, 11, 1))
        self.assertEqual([p["key"] for p in posts], ["", ""])  # 只搜一次全量，翻到 since 之前停
        self.assertEqual(len(notices), 2 * airport.PAGE_SIZE)

    def test_ceair_archive_mode_lists_all_and_filters_outside(self):
        posts = []

        def post(url, data=None, headers=None):
            posts.append(data)
            res = [{"id": "1", "title": "东航总部办公楼保洁服务采购项目", "publishTime": "2026-11-02 10:00:00"},
                   {"id": "2", "title": "东航山西分公司车辆维修项目", "publishTime": "2026-11-02 09:00:00"}]
            return FakeResponse({"flag": "T", "param1": {"result": res, "totalPages": 1}})

        session = type("S", (), {"post": staticmethod(post), "interval": 0})()
        notices = ceair.fetch(session, CFG, date(2026, 11, 1))
        self.assertEqual([(p["title"], p["pageSize"]) for p in posts], [("", ceair.ARCHIVE_PAGE_SIZE)])
        self.assertEqual(session.interval, ceair.ARCHIVE_INTERVAL)
        self.assertEqual([n.source_id for n in notices], ["1"])


    def test_rate_limited_central_search_keeps_insurance_results(self):
        insurance = ('<p>共找到<span>1</span>条</p><ul class="vT-srch-result-list-bid"><li>'
                     '<a href="http://www.ccgp.gov.cn/cggg/zygg/gkzb/202611/t20261103_1.htm">某局车辆保险采购公告</a>'
                     '<span>2026.11.03 10:00:00 | 采购人：某局 | 代理机构：某代理 <strong>公开招标公告</strong> | 上海</span>'
                     '</li></ul>')

        def fake_get(session, url, **kwargs):
            params = kwargs.get("params")
            if params is None:
                return "<html>首页</html>"
            return insurance if params["kw"] else "您的访问过于频繁,请稍后再试。"

        session = type("S", (), {"interval": 0, "headers": {}})()
        state = {"central_last_ok": "2026-11-01"}
        with mock.patch.object(ccgp, "_get", side_effect=fake_get), mock.patch.object(ccgp.time, "sleep"):
            notices = ccgp.fetch(session, CFG, date(2026, 10, 15), state)
        self.assertEqual([n.title for n in notices], ["某局车辆保险采购公告"])
        # 中央单位全量搜索被限流时，不推进 central_last_ok，下次运行继续从上次成功处补抓
        self.assertEqual(state["central_last_ok"], "2026-11-01")
        self.assertIn("ins_last_ok", state)

    def test_ccgp_same_day_central_uses_today_window(self):
        today = date.today()
        seen_params = []
        empty = '<p>共找到<span>0</span>条</p><script>Pager({size:1, current:0})</script>'

        def fake_get(session, url, **kwargs):
            params = kwargs.get("params")
            if params is not None:
                seen_params.append(params)
                return empty
            return "<html>首页</html>"

        session = type("S", (), {"interval": 0, "headers": {}})()
        state = {"ins_last_ok": today.isoformat(), "central_last_ok": today.isoformat()}
        with mock.patch.object(ccgp, "_get", side_effect=fake_get), mock.patch.object(ccgp.time, "sleep"):
            ccgp.fetch(session, CFG, today - timedelta(days=30), state)
        # 保险搜索保持 7 天回看窗口，而同日第二轮中央单位全量搜索只查今天，避免两日叠加超 6 页
        self.assertEqual(seen_params[0]["start_time"], (today - timedelta(days=ccgp.INSURANCE_LOOKBACK_DAYS)).strftime("%Y:%m:%d"))
        self.assertEqual(seen_params[1]["start_time"], today.strftime("%Y:%m:%d"))

class EbscnArchiveTest(unittest.TestCase):
    def page(self, n):
        cards = "".join(
            f'<a class="ch_bidding" href="/web/gdweb/detail.do?articleId=a{i}">'
            f'<span class="ch_bid_span">{"员工补充医疗保险采购项目" if i == n - 1 else f"服务器维保项目{i}"}</span></a>'
            for i in range(n))
        return f'<ul class="ch_con">{cards}</ul>'

    def test_detail_budget_and_cache(self):
        details = []

        class Resp:
            def __init__(self, text): self.text, self.encoding = text, None
            def raise_for_status(self): pass

        def post(url, data=None, headers=None):
            self.assertEqual(data["article.searchName"], "")
            return Resp(self.page(25))

        def get(url, headers=None):
            details.append(url)
            return Resp('<div class="tender_detail">2026年11月1日</div>')

        session = type("S", (), {"post": staticmethod(post), "get": staticmethod(get)})()
        state = {"dates": {"a0": "2026-10-31"}}  # a0 已缓存，不再打开详情
        notices = ebscn.fetch(session, CFG, date(2026, 11, 1), state)
        # 23 条新的非保险公告只开 20 个详情；保险那条（a24）不占额度
        self.assertEqual(len(details), ebscn.NEW_DETAILS_PER_RUN + 1)
        self.assertIn("a24", {n.source_id for n in notices})
        self.assertEqual(len(notices), 1 + ebscn.NEW_DETAILS_PER_RUN + 1)
        self.assertEqual(len(state["dates"]), len(notices))


class BaowuArchiveTest(unittest.TestCase):
    def test_archive_mode_uses_snippet_and_no_detail_requests(self):
        sent = []

        class Resp:
            def __init__(self, text): self.text, self.encoding = text, None
            def raise_for_status(self): pass

        def post(url, data=None, headers=None):
            sent.append(json.loads(data))
            records = [
                {"id": "sh", "title": "热轧厂设备检修项目招标公告", "webdate": "2026-11-02 09:00:00",
                 "linkurl": "/jyxx/sh.html", "content": "招标人：宝山钢铁股份有限公司 项目地点：上海市宝山区"},
                {"id": "far", "title": "大冶矿外运站整改项目招标公告", "webdate": "2026-11-02 09:00:00",
                 "linkurl": "/jyxx/far.html", "content": "招标人：大冶特钢 项目地点：湖北省"},
            ]
            inner = {"result": {"totalcount": 2, "records": records}}
            return Resp(json.dumps({"code": 200, "content": json.dumps(inner, ensure_ascii=False)}, ensure_ascii=False))

        def get(url, headers=None):
            raise AssertionError("非保险项目不该打开详情页")

        session = type("S", (), {"post": staticmethod(post), "get": staticmethod(get)})()
        notices = baowu.fetch(session, CFG, date(2026, 11, 1))
        self.assertEqual(len(sent), 1)
        self.assertNotIn("searchtitle", [c["fieldName"] for c in sent[0]["condition"]])
        self.assertEqual(sent[0]["cl"], 800)
        self.assertEqual([n.source_id for n in notices], ["sh"])


class CcgpArchiveTest(unittest.TestCase):
    def test_archive_mode_adds_central_unit_search(self):
        searches = []

        def fake_get(session, url, **kwargs):
            params = kwargs.get("params")
            if params is None:
                return "<html>首页</html>"
            searches.append((params["kw"], params["bidSort"]))
            return "<p>共找到<span>0</span>条</p>"

        session = type("S", (), {"interval": 0, "headers": {}})()
        with mock.patch.object(ccgp, "_get", side_effect=fake_get), mock.patch.object(ccgp.time, "sleep"):
            ccgp.fetch(session, CFG, date(2026, 11, 1))
        self.assertEqual(searches, [("保险", "0"), ("", "1")])

if __name__ == "__main__":
    unittest.main()
