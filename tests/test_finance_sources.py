"""上交所采购门户（sse）和金采网（cfcpn）两个来源的测试，不联网。"""

import json
import unittest
from datetime import date

from tender_alert.config import Config
from tender_alert.filters import match
from tender_alert.sources import cfcpn, sse


def cfg(archive=True):
    return Config(
        keywords=["保险", "车险", "补充医疗"], exclude=[], regions=["上海"],
        kinds=["tender", "correction", "other"], lookback_days=30, sources=[],
        request_interval=0, failure_threshold=3, archive=archive,
    )


class FakeResponse:
    def __init__(self, text):
        self.text = text
        self.encoding = None

    def raise_for_status(self):
        pass

    def json(self):
        return json.loads(self.text)


SSE_HTML = """
<div class="sse_list_1 js_listPage" id="sse_list_1"><dl>
  <dd><span> 2026-11-10 </span>
    <a href="/purchase/information/c/c_20261109_10837201.shtml" title="供应商征集公告（2026年数据公司公务用车车险服务采购项目）" target="_blank">
      供应商征集公告（2026年数据公司公务用车车险服务采购项目） </a></dd>
  <dd><span> 2026-10-29 </span>
    <a href="/purchase/information/c/c_20261029_10836096.shtml" title="采购意向公开（11月）">采购意向公开（11月）</a></dd>
  <dd><span> 2026-06-25 </span>
    <a href="/purchase/information/c/c_20260624_10823344.shtml" title="供应商征集公告（2026-2028年信息公司公务车维保项目）">x</a></dd>
</dl></div>
"""


class SseSession:
    interval = 0
    headers: dict = {}

    def __init__(self, html):
        self.html = html
        self.urls = []

    def get(self, url, **kw):
        self.urls.append(url)
        return FakeResponse(self.html)


class SseTest(unittest.TestCase):
    def test_parses_list_and_drops_old(self):
        session = SseSession(SSE_HTML)
        notices = sse.fetch(session, cfg(), date(2026, 10, 12))
        self.assertEqual(session.urls, [sse.LIST_URL])
        self.assertEqual([n.source_id for n in notices], ["10837201", "10836096"])
        n = notices[0]
        self.assertEqual(n.title, "供应商征集公告（2026年数据公司公务用车车险服务采购项目）")
        self.assertEqual(n.url, "https://www.sse.com.cn/purchase/information/c/c_20261109_10837201.shtml")
        self.assertEqual(n.publish_date, "2026-11-10")
        self.assertEqual(n.purchaser, "上海证券交易所")

    def test_insurance_notice_matches_after_classification(self):
        from tender_alert.models import classify_kind
        n = sse.fetch(SseSession(SSE_HTML), cfg(), date(2026, 10, 12))[0]
        n.kind = classify_kind(n.notice_type, n.title)
        self.assertEqual(n.kind, "tender")  # 「征集」算招标类
        self.assertEqual(match(n, cfg(), sse.REGION_SCOPED), ["车险"])

    def test_layout_change_raises(self):
        with self.assertRaises(RuntimeError):
            sse.fetch(SseSession("<html><body>维护中</body></html>"), cfg(), date(2026, 10, 12))


def row(nid, title, day="2026-11-10", area="上海市", code="310000", method="公开招标", buyer="某银行"):
    return {"id": nid, "noticeTitle": title, "publishTime": f"{day} 10:01:43.0", "area": area,
            "areaProvince": code, "purchaseTypeName": method, "userName": buyer}


class CfcpnSession:
    """按查询条件返回对应的结果页。"""

    def __init__(self, by_area=(), by_title=(), raw=None):
        self.interval = 0
        self.headers = {}
        self.by_area, self.by_title, self.raw = list(by_area), list(by_title), raw
        self.calls = []

    def post(self, url, data=None, headers=None):
        self.calls.append(dict(data))
        if self.raw is not None:
            return FakeResponse(self.raw)
        rows = self.by_area if data["areaProvince"] else self.by_title
        start = (data["pageNo"] - 1) * data["pageSize"]
        return FakeResponse(json.dumps({"result": "", "total": len(rows), "rows": rows[start:start + data["pageSize"]]}))


class CfcpnTest(unittest.TestCase):
    SINCE = date(2026, 11, 8)

    def test_area_and_title_queries_combined(self):
        session = CfcpnSession(
            by_area=[
                row("a1", "26LH001862;太保寿险信创笔记本采购项目公告", method="其他"),
                row("a2", "国泰海通证券2026年投标分销系统建设采购项目中标公告"),
                row("a3", "上海市静安区某医院&ldquo;智慧医院&rdquo;项目中标结果公示", method="其他"),
            ],
            by_title=[
                row("a3", "上海市静安区某医院&ldquo;智慧医院&rdquo;项目中标结果公示", method="其他"),
                row("t1", "北京银行上海分行2026年员工补充医疗保险采购项目招标公告", area="北京市", code="110000"),
                row("t2", "上海浦东发展银行股份有限公司杭州分行2026年绍兴职工疗休养项目招标公告", area="浙江省", code="330000"),
                row("t3", "上海银行盐城分行2026-2029年保安服务外包项目失败公告", area="江苏省", code="320000"),
            ],
        )
        notices = {n.source_id: n for n in cfcpn.fetch(session, cfg(), self.SINCE)}
        self.assertEqual(sorted(notices), ["a1", "a2", "a3", "t1"])  # 外地分行去掉，重复的只留一条
        self.assertEqual(session.interval, cfcpn.MIN_INTERVAL)
        self.assertEqual(notices["a1"].title, "太保寿险信创笔记本采购项目公告")  # 去掉内部编号
        self.assertEqual(notices["a3"].title, "上海市静安区某医院“智慧医院”项目中标结果公示")  # HTML 实体还原
        self.assertEqual(notices["a2"].kind, "result")  # 采购方式写「公开招标」，但标题是中标公告
        self.assertEqual(notices["t1"].kind, "tender")
        self.assertEqual(notices["t1"].publish_date, "2026-11-10 10:01")
        self.assertIn("searchVal=t1", notices["t1"].url)
        self.assertEqual(match(notices["t1"], cfg(), cfcpn.REGION_SCOPED), ["保险", "补充医疗"])
        area_call, title_call = session.calls
        self.assertEqual((area_call["areaProvince"], area_call["noticeTitle"]), ("310000", ""))
        self.assertEqual((title_call["areaProvince"], title_call["noticeTitle"]), ("", "上海"))
        self.assertEqual(area_call["beginPublishTime"], "2026-11-08")

    def test_pages_until_total(self):
        rows = [row(f"n{i}", f"上海某银行第{i}号采购项目招标公告") for i in range(120)]
        session = CfcpnSession(by_area=rows)
        notices = cfcpn.fetch(session, cfg(), self.SINCE)
        self.assertEqual(len(notices), 120)
        self.assertEqual([c["pageNo"] for c in session.calls if c["areaProvince"]], [1, 2, 3])

    def test_never_more_than_three_pages_per_query(self):
        # 接口连续请求六七次就返回 403，所以每种查法最多 3 页
        rows = [row(f"n{i}", f"上海某银行第{i}号采购项目招标公告") for i in range(400)]
        session = CfcpnSession(by_area=rows, by_title=rows)
        notices = cfcpn.fetch(session, cfg(), self.SINCE)
        self.assertEqual(len(notices), 150)
        self.assertEqual(len(session.calls), 6)

    def test_rate_limited_midway_keeps_what_it_has(self):
        import requests

        class Limited(CfcpnSession):
            def post(self, url, data=None, headers=None):
                if data["pageNo"] >= 2:
                    resp = requests.Response()
                    resp.status_code = 403
                    self.calls.append(dict(data))
                    return resp
                return super().post(url, data, headers)

        rows = [row(f"n{i}", f"上海某银行第{i}号采购项目招标公告") for i in range(120)]
        with self.assertLogs(cfcpn.log, "WARNING"):
            notices = cfcpn.fetch(Limited(by_area=rows), cfg(), self.SINCE)
        self.assertEqual(len(notices), 50)

    def test_refused_on_first_page_is_a_failure(self):
        import requests

        class Blocked(CfcpnSession):
            def post(self, url, data=None, headers=None):
                resp = requests.Response()
                resp.status_code = 403
                return resp

        with self.assertRaises(requests.HTTPError):
            cfcpn.fetch(Blocked(), cfg(), self.SINCE)

    def test_without_archive_searches_keywords_only(self):
        session = CfcpnSession(by_area=[row("a1", "某银行上海分行车辆保险采购公告")])
        cfcpn.fetch(session, cfg(archive=False), self.SINCE)
        self.assertEqual([c["noticeTitle"] for c in session.calls], ["险", "医疗保障", "上海"])

    def test_insurer_buying_other_things_is_not_a_hit(self):
        session = CfcpnSession(by_area=[row("a1", "新华人寿保险股份有限公司上海分公司2026年旅游服务入围供应商库招标公告")])
        n = cfcpn.fetch(session, cfg(), self.SINCE)[0]
        self.assertIsNone(match(n, cfg(), cfcpn.REGION_SCOPED))

    def test_bad_response_raises(self):
        with self.assertRaises(RuntimeError):
            cfcpn.fetch(CfcpnSession(raw='{"msg": "系统错误"}'), cfg(), self.SINCE)


if __name__ == "__main__":
    unittest.main()
