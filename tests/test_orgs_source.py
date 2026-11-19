"""上海企事业单位自有采购网站来源（orgs）的测试，不联网。"""

import json
import unittest
from datetime import date
from unittest import mock

from tender_alert.config import Config
from tender_alert.sources import edu, orgs

CFG = Config(keywords=["保险"], exclude=[], regions=["上海"], kinds=["tender"], lookback_days=30,
             sources=[], request_interval=0, failure_threshold=3, archive=True)
SINCE = date(2026, 10, 19)


class FakeResponse:
    def __init__(self, text, status=200):
        self.text, self.status, self.encoding, self.apparent_encoding = text, status, "utf-8", "utf-8"

    def raise_for_status(self):
        if self.status >= 400:
            raise RuntimeError(f"HTTP {self.status}")

    def json(self):
        return json.loads(self.text)


class FakeSession:
    interval = 0
    headers: dict = {}

    def __init__(self, pages, posts=None):
        self.pages = pages
        self.post_map = posts or {}
        self.posts = []

    def mount(self, prefix, adapter):
        pass

    def get(self, url, **kw):
        return self.pages[url]

    def post(self, url, json=None, headers=None, **kw):
        self.posts.append((url, json))
        if url in self.post_map:
            val = self.post_map[url]
            return val(json) if callable(val) else val
        raise KeyError(f"unexpected POST: {url}")


ROW = '<li><span>{day}</span><a href="{href}">{title}</a></li>'
TRUST_OK = FakeResponse(json.dumps({"success": True, "result": {"records": [
    {"id": "211", "title": "上海信托2027年度员工补充医疗保险采购项目竞争性磋商公告", "publishTime": "2026-11-13"},
    {"id": "212", "title": "上海国际信托有限公司一般关联交易信息披露 （2026年第三季度）", "publishTime": "2026-10-28"},
    {"id": "200", "title": "很早的采购结果公告", "publishTime": "2026-08-11"},
]}}))
SITC_TENDER = FakeResponse(json.dumps({"current_page": 1, "data": [
    {"fdId": "901", "fdSubject": "【2604001】光明牧业车辆保险采购招标公告", "publishTime": "2026-11-18", "fdEndTime": "2026-11-25"},
    {"fdId": "902", "fdSubject": "某集团南京分公司设备采购招标公告", "publishTime": "2026-11-18", "fdEndTime": "2026-11-25"},
]}))
SITC_RESULT = FakeResponse(json.dumps({"current_page": 1, "data": [
    {"fdId": "903", "fdSubject": "【2603009】上海某医院医疗责任险中标结果公告", "publishTime": "2026-11-15", "fdEndTime": ""},
]}))


def cwcc_reply(payload):
    t = int(payload["typeindex"])
    rows = {
        1: [
            {"Id": 20513, "Name": "中国工商银行江苏省分行宣传印刷品采购项目招标公告", "Date": "2026/11/17 15:24:39"},
            {"Id": 20502, "Name": "中汇信息技术（上海）有限公司2027年员工补充医疗保险招标公告", "Date": "2026/11/13 15:11:08"},
        ],
        2: [{"Id": 19001, "Name": "上海某高校设备项目中标公告", "Date": "2026/11/10 10:00:00"}],
        3: [{"Id": 8001, "Name": "上海某医院公众责任险竞争性磋商公告", "Date": "2026/11/12 09:30:00"}],
    }[t]
    return FakeResponse(json.dumps({"d": json.dumps({"status": 1, "sumcount": len(rows), "infos": rows})}))


def default_posts(trust=TRUST_OK):
    t1_url = f"{orgs.SITC_API}?method=getNotice&type=1&templateId={orgs.SITC_TYPES[0][1]}&showNotice=all&seacrhVal="
    t3_url = f"{orgs.SITC_API}?method=getNotice&type=3&templateId={orgs.SITC_TYPES[1][1]}&showNotice=all&seacrhVal="
    return {
        orgs.TRUST_API: trust,
        t1_url: SITC_TENDER,
        t3_url: SITC_RESULT,
        orgs.CWCC_API: cwcc_reply,
    }


class OrgsTest(unittest.TestCase):
    def run_fetch(self, pages, posts=None, org_list=None, api_list=None):
        org_list = org_list if org_list is not None else [
            ("aa", "甲单位", ["https://a.example.cn/list.htm"], False),
            ("tb", "某全国平台", ["https://t.example.cn/list.htm"], True),
            ("up", "某在沪总部平台", ["https://u.example.cn/list.htm"], "local"),
        ]
        session = FakeSession(pages, default_posts() if posts is None else posts)
        with mock.patch.object(orgs, "ORGS", org_list), \
                mock.patch.object(orgs, "API_ORGS", api_list if api_list is not None else orgs.API_ORGS):
            return {n.source_id: n for n in orgs.fetch(session, CFG, SINCE)}, session

    def test_static_orgs_and_three_json_apis(self):
        pages = {
            "https://a.example.cn/list.htm": FakeResponse(ROW.format(day="2026-11-16", href="/n/1.htm",
                                                                     title="关于2027年甲单位车辆保险服务供应商比选的公告")),
            "https://t.example.cn/list.htm": FakeResponse(
                ROW.format(day="2026-11-18", href="/z/1.jhtml", title="上海烟草集团有限责任公司车辆保险服务-公开招标公告")
                + ROW.format(day="2026-11-18", href="/z/2.jhtml", title="江西中烟工业有限责任公司食堂服务外包项目公开招标公告")),
            "https://u.example.cn/list.htm": FakeResponse(
                ROW.format(day="2026-11-17", href="/u/1", title="【更正公告】中国银联2026年员工团体保险采购项目采购公告")
                + ROW.format(day="2026-11-17", href="/u/2", title="中国银联广东分公司软件开发采购项目采购公告")),
        }
        notices, session = self.run_fetch(pages)
        self.assertEqual(
            sorted(notices),
            ["aa:/n/1.htm", "cwcc:1-20502", "cwcc:2-19001", "cwcc:3-8001",
             "shtrust:211", "sitc:901", "sitc:903", "tb:/z/1.jhtml", "up:/u/1"],
        )
        self.assertEqual(notices["aa:/n/1.htm"].purchaser, "甲单位")
        self.assertEqual(notices["shtrust:211"].url, "https://www.shanghaitrust.com/node?id=211")
        self.assertEqual(notices["sitc:901"].extra, {"deadline": "2026-11-25"})
        self.assertEqual(notices["sitc:903"].kind, "result")
        self.assertEqual(notices["cwcc:1-20502"].publish_date, "2026-11-13")
        self.assertEqual(notices["cwcc:2-19001"].kind, "result")
        self.assertEqual(session.posts[0][1]["columnCode"], "information_disclosure")

    def test_one_org_down_is_only_a_warning(self):
        pages = {
            "https://a.example.cn/list.htm": FakeResponse("拦截", status=504),
            "https://t.example.cn/list.htm": FakeResponse(ROW.format(day="2026-11-18", href="/z/1.jhtml",
                                                                     title="上海烟草集团车辆保险服务公开招标公告")),
        }
        orgs_list = [("aa", "甲单位", ["https://a.example.cn/list.htm"], False),
                     ("tb", "某全国平台", ["https://t.example.cn/list.htm"], True),
                     ("cc", "丙单位", ["https://t.example.cn/list.htm"], False)]
        with self.assertLogs(orgs.log, "WARNING") as logs:
            notices, session = self.run_fetch(pages, org_list=orgs_list, api_list=[orgs.API_ORGS[0]])
        self.assertIn("甲单位", logs.output[0])
        self.assertIn("tb:/z/1.jhtml", notices)
        # 单个子单位失败会写进 session.sub_health，供主流程独立计入连续失败告警
        self.assertIsNotNone(session.sub_health["orgs:aa"][1])
        self.assertIsNone(session.sub_health["orgs:tb"][1])
        self.assertIsNone(session.sub_health["orgs:shtrust"][1])

    def test_sub_unit_display_name(self):
        from tender_alert.sources import source_display_name
        self.assertEqual(source_display_name("orgs:unionpay"), "上海企事业单位·自有采购网站 · 中国银联")
        self.assertEqual(source_display_name("edu:fudan"), "上海高校·采购招标公告 · 复旦大学")
        self.assertEqual(source_display_name("zfcg_sh"), "上海政府采购网")

    def test_many_failures_raise(self):
        broken = FakeResponse("<html>维护中</html>")
        pages = {"https://a.example.cn/list.htm": broken, "https://t.example.cn/list.htm": broken,
                 "https://u.example.cn/list.htm": broken}
        bad_json = FakeResponse('{"success": false, "result": null}')
        with self.assertLogs(orgs.log, "WARNING"), self.assertRaises(RuntimeError):
            self.run_fetch(pages, posts={orgs.TRUST_API: bad_json})

    def test_keys_unique(self):
        keys = [k for k, _, _, _ in orgs.ORGS] + [k for k, _, _ in orgs.API_ORGS]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertEqual(len(keys), 21)


class ParserTitleTest(unittest.TestCase):
    """第五批及后续网站带来的标题与日期写法。"""

    def test_title_and_date_side_by_side_inside_link(self):
        # 中银国际：<a><p>标题</p><span>日期</span></a>
        items = edu.parse_list('<ul id="articlelist"><li><a href="/main/a/20261113/1.shtml">'
                               '<p>中银国际证券股份有限公司2027年网络设备维保采购项目采购邀请公告</p>'
                               '<span class="time">2026-11-13</span></a></li></ul>', "https://www.bocichina.com/x")
        self.assertEqual(items, [("中银国际证券股份有限公司2027年网络设备维保采购项目采购邀请公告",
                                  "https://www.bocichina.com/main/a/20261113/1.shtml", "2026-11-13")])

    def test_full_title_in_descendant_title_attribute(self):
        # 中烟平台：<a><p><em>服务</em><span title="完整标题">…</span></p><p>发布日期: 2026-11-18</p></a>
        items = edu.parse_list('<ul><li><a href="/zcfwzb/1.jhtml"><p><em class="fuwu">服务</em>'
                               '<span title="上海烟草集团车辆保险服务项目公开招标公告">上海烟草集团车辆保险服务项…</span></p>'
                               '<p><i>发布日期: &nbsp; 2026-11-18</i> 详情</p></a></li></ul>', "https://cgjy.tobacco.com.cn/x")
        self.assertEqual(items[0][0], "上海烟草集团车辆保险服务项目公开招标公告")
        self.assertEqual(items[0][2], "2026-11-18")

    def test_hospital_needs_survey_counts(self):
        items = edu.parse_list('<li><a href="/c/1"><span>2026-09-02</span><h3>复旦大学附属肿瘤医院医疗责任险院内需求调查公告</h3></a></li>',
                               "https://www.shca.org.cn/x")
        self.assertEqual(items[0][2], "2026-09-02")

    def test_two_digit_year_and_iconfont_noise(self):
        # 上海黄金交易所：<a class="title"><i>\ue60f</i><span class="txt">标题</span><span> 26-11-18</span></a>
        items = edu.parse_list(
            '<li><a class="title" href="/gyjjs/cgzq/10007673"><i>\ue60f</i>'
            '<span class="txt">上海黄金交易所员工补充医疗保险采购结果公告</span>'
            '<i>\ue63a</i><span class="fr"> 26-11-18</span></a></li>',
            "https://www.sge.com.cn/gyjjs/cgzq",
        )
        self.assertEqual(items, [(
            "上海黄金交易所员工补充医疗保险采购结果公告",
            "https://www.sge.com.cn/gyjjs/cgzq/10007673",
            "2026-11-18",
        )])

    def test_trs_wcm_url_date(self):
        # 上海清算所：行内日期由前端脚本渲染，静态 HTML 里日期在文章路径 /t20261028_xxx.html
        items = edu.parse_list(
            '<div class="news_details"><a href="./202610/t20261028_1893334.html">'
            '<p>银行间市场清算所股份有限公司员工团体医疗保险采购项目采购结果公告</p></a></div>',
            "https://www.shclearing.com.cn/xwgg/zcxx/",
        )
        self.assertEqual(items, [(
            "银行间市场清算所股份有限公司员工团体医疗保险采购项目采购结果公告",
            "https://www.shclearing.com.cn/xwgg/zcxx/202610/t20261028_1893334.html",
            "2026-10-28",
        )])

    def test_decode_html_and_stop_before(self):
        from tender_alert.sources._common import decode_html

        class RawResp:
            encoding = "ISO-8859-1"
            content = '<html><head><meta charset="gbk"></head><body>中文采购公告</body></html>'.encode("gbk")
            @property
            def apparent_encoding(self):
                raise AssertionError("有 meta charset 时不应触发全文嗅探")

        self.assertIn("中文采购公告", decode_html(RawResp()))
        # 超长倒序列表遇到连续 40 条早于 stop_before 的记录时提前停下，同时保留顶部置顶旧公告后的新公告
        pinned_old = ROW.format(day="2024-01-01", href="/p0.htm", title="置顶的历史采购公告样本")
        recent = ROW.format(day="2026-11-15", href="/new.htm", title="最新车辆保险采购公告")
        many_old = "".join(ROW.format(day="2025-05-01", href=f"/old{i}.htm", title=f"历史设备采购公告{i}") for i in range(60))
        items = edu.parse_list(f"<ul>{pinned_old}{recent}{many_old}</ul>", "https://caigou.smg.cn/x", stop_before="2026-10-19")
        self.assertIn("最新车辆保险采购公告", [t for t, _, _ in items])
        self.assertLess(len(items), 50)


if __name__ == "__main__":
    unittest.main()
