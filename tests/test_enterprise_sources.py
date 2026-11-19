import unittest
from datetime import date

from tender_alert.config import Config
from tender_alert.sources import bosc, ceair, gtht
from tender_alert.sources._common import outside_shanghai

CFG = Config(keywords=["保险", "责任险"], exclude=[], regions=["上海"], kinds=["tender", "correction", "other"],
             lookback_days=30, sources=[], request_interval=0, failure_threshold=3)
SINCE = date(2026, 9, 30)


class FakeResponse:
    def __init__(self, text="", data=None):
        self.text, self._data = text, data
        self.encoding = None

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


class FakeSession:
    """按 (方法, URL) 返回预先准备好的响应，记录请求。"""

    def __init__(self, responses):
        self.responses, self.requests = responses, []

    def get(self, url, params=None, headers=None):
        self.requests.append(("GET", url, params))
        return self.responses[("GET", url)]

    def post(self, url, data=None, headers=None):
        self.requests.append(("POST", url, data))
        resp = self.responses[("POST", url)]
        return resp(data) if callable(resp) else resp

    def mount(self, prefix, adapter):
        pass


class OutsideShanghaiTest(unittest.TestCase):
    def test_cases(self):
        cases = {
            "东航山西2027-2029内场车辆保险采购项目": True,
            "北京大兴国际机场东航基地项目核心工作区K-01-01地块工程保险项目": True,
            "上海银行天津分行2026-2029年授信业务财产保险服务采购": True,
            "东航技术西北分公司机库设施设备托管及维保项目": True,
            "2026年上海审定中心保险服务项目": False,
            "2026年东航员工综合医疗保障采购项目": False,
            "上海银行2026-2028年度员工补充医疗保险采购项目招标公告": False,
            "上海银行南京路支行装修项目": False,  # 南京路是上海的路
        }
        for title, outside in cases.items():
            self.assertEqual(outside_shanghai(title), outside, title)


class CeairTest(unittest.TestCase):
    def test_search_filters_outside_and_maps_fields(self):
        def respond(data):
            rows = {
                "险": [
                    {"id": "a1", "title": "2026年东航员工综合医疗保障采购项目", "publishTime": "2026-10-20 10:00:00",
                     "endTime": "20261110133000"},
                    {"id": "a2", "title": "东航山西2027-2029内场车辆保险采购项目", "publishTime": "2026-10-19 10:00:00",
                     "endTime": None},
                ],
                "医疗保障": [
                    {"id": "a1", "title": "2026年东航员工综合医疗保障采购项目", "publishTime": "2026-10-20 10:00:00",
                     "endTime": "20261110133000"},
                ],
            }[data["title"]]
            return FakeResponse(data={"flag": "T", "param1": {"result": rows, "totalPages": 1}})

        session = FakeSession({("POST", ceair.API): respond})
        session.interval = 0
        notices = ceair.fetch(session, CFG, SINCE)
        self.assertEqual([r[2]["title"] for r in session.requests], ["险", "医疗保障"])  # 只搜两次，避开频率限制
        self.assertEqual([n.source_id for n in notices], ["a1"])
        n = notices[0]
        self.assertEqual(n.url, "https://caigou.ceair.com/portal/portal/noticeDetails?id=a1")
        self.assertEqual(n.publish_date, "2026-10-20 10:00")
        self.assertEqual(n.extra["deadline"], "2026-11-10 13:30")

    def test_api_failure_raises(self):
        session = FakeSession({("POST", ceair.API): FakeResponse(data={"flag": "F", "param1": "操作频繁，请6分钟后重试！"})})
        session.interval = 0
        with self.assertRaises(RuntimeError):
            ceair.fetch(session, CFG, SINCE)


def bosc_page(items):
    lis = "".join(
        f'<li><a href="./{d[:4]}{d[5:7]}/t{d.replace("-", "")}_{i}.shtml" title="{t}">{t}</a></li>'
        for i, t, d in items
    )
    return f'<a href="/zh/xyk/fqyw/202004/t20200426_13189.shtml">账单分期</a><ul>{lis}</ul>'


class BoscTest(unittest.TestCase):
    def test_first_page_stops_at_seen_and_filters_branches(self):
        session = FakeSession({("GET", bosc.LIST_URL): FakeResponse(bosc_page([
            ("3", "上海银行2026-2028年度员工补充医疗保险采购项目招标公告", "2026-10-30"),
            ("2", "上海银行天津分行2026-2029年授信业务财产保险服务采购", "2026-10-29"),
            ("1", "上海银行鸿蒙APP认证检测项目供应商征集公告", "2026-10-28"),
        ]))})
        state = {"seen": ["1"]}
        notices = bosc.fetch(session, CFG, SINCE, state)
        self.assertEqual(len(session.requests), 1)  # 第 1 页就有看过的，不再翻页
        self.assertEqual([n.source_id for n in notices], ["3", "1"])
        self.assertEqual(notices[0].url, "https://www.bosc.cn/zh/jrsh/xxgk_1091/tzzxgqk/zbcg/202610/t20261030_3.shtml")
        self.assertEqual(notices[0].publish_date, "2026-10-30")
        self.assertEqual(state["seen"][:3], ["3", "2", "1"])

    def test_empty_first_page_raises(self):
        session = FakeSession({("GET", bosc.LIST_URL): FakeResponse("<html>改版了</html>")})
        with self.assertRaises(RuntimeError):
            bosc.fetch(session, CFG, SINCE, {})


class GthtTest(unittest.TestCase):
    def test_maps_fields_and_detail_url(self):
        data = {"code": "0", "data": {"total": 2, "list": [
            {"id": "x1", "projectId": "p1", "title": "国泰海通证券2026年财产一切险和公众责任险采购项目竞争性磋商公告",
             "publishTime": "2026-10-09"},
            {"id": "x2", "projectId": "", "title": "关于更新采购供应商联系人的提示", "publishTime": "2026-08-24"},
        ]}}
        session = FakeSession({("GET", gtht.API): FakeResponse(data=data)})
        notices = gtht.fetch(session, CFG, SINCE)
        self.assertEqual(session.requests[0][2]["title"], "险")
        self.assertEqual(session.requests[0][2]["noticeFlag"], "01")  # 默认不提醒结果，不查结果公告
        n = notices[0]
        self.assertEqual(n.url, "https://supplier.gtht.com/#/procurnotice?id=x1&projectId=p1&noticeFlag=true")
        self.assertEqual(n.kind, "other")

    def test_api_failure_raises(self):
        session = FakeSession({("GET", gtht.API): FakeResponse(data={"code": "500", "msg": "error"})})
        with self.assertRaises(RuntimeError):
            gtht.fetch(session, CFG, SINCE)


if __name__ == "__main__":
    unittest.main()
