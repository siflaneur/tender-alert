"""上海高校来源（edu）的测试：通用列表解析器要认得各校不同的网页写法。不联网。"""

import unittest
from datetime import date
from unittest import mock

from tender_alert.config import Config
from tender_alert.sources import edu

CFG = Config(keywords=["保险"], exclude=[], regions=["上海"], kinds=["tender"], lookback_days=30,
             sources=[], request_interval=0, failure_threshold=3, archive=True)
BASE = "https://zb.example.edu.cn/1310/list.htm"


class ParseListTest(unittest.TestCase):
    def one(self, html):
        items = edu.parse_list(html, BASE)
        self.assertEqual(len(items), 1, items)
        return items[0]

    def test_plain_row_with_truncated_text(self):
        # 最常见：一行一个 li，正文被截断，完整标题在 title 属性里
        title, url, day = self.one("""<ul><li><span>2026-11-10</span>
            <a href="/2026/1110/c1a2/page.htm" title="上海某大学高速摄像机采购公告">上海某大学高速摄像机采...</a></li></ul>""")
        self.assertEqual((title, day), ("上海某大学高速摄像机采购公告", "2026-11-10"))
        self.assertEqual(url, "https://zb.example.edu.cn/2026/1110/c1a2/page.htm")

    def test_title_attribute_holding_a_url_is_ignored(self):
        # 上海第二工业大学：title 属性里放的是网址
        title, _, day = self.one("""<li><span>2026-10-21</span>
            <a href="/a/page.htm" title="/a/page.htm">卫星姿态可视化模拟软件单一来源公示</a></li>""")
        self.assertEqual((title, day), ("卫星姿态可视化模拟软件单一来源公示", "2026-10-21"))

    def test_whole_row_inside_link_uses_publish_date_not_summary_date(self):
        # 上海科技大学：整行包在链接里，摘要里还有别的日期
        title, _, day = self.one("""<li><a href="/2026/1110/c1428a1/page.htm">
            <div class="news_title">上海科技大学实验室水系统采购询价结果公告</div>
            <div class="news_text">询价日期：2026-11-08 如有异议请于三日内提出，公示期满不再另行公告</div>
            <div class="news_meta"><span class="t">2026-11-10</span></div></a></li>""")
        self.assertEqual((title, day), ("上海科技大学实验室水系统采购询价结果公告", "2026-11-10"))

    def test_date_split_into_day_and_year_month(self):
        # 上海中医药大学：「11」和「2026.11」分开放，标题里还带着别的日期
        title, _, day = self.one("""<li><a href="/p.htm"><div class="mtdate"><span>09</span><p>2026.11</p></div>
            <div><h2>【资产管理处】2026.11.5上海中医药大学仪器设备询价表</h2><p>了解详情</p></div></a></li>""")
        self.assertEqual((title, day), ("【资产管理处】2026.11.5上海中医药大学仪器设备询价表", "2026-11-09"))

    def test_month_day_before_year(self):
        # 上海应用技术大学：「11-09」「2026」
        _, _, day = self.one("""<li><a href="info/1005/1.htm" title="功能材料实验室建设的公开招标公告">
            <div class="tl-data"><p>11-09</p><span>2026</span></div><h3>功能材料实验室建设的公开招...</h3></a></li>""")
        self.assertEqual(day, "2026-11-09")

    def test_row_without_year_takes_date_from_article_url(self):
        # 上海海事大学：行里只有「11月03日」，年份在文章地址里
        title, _, day = self.one("""<li><a href="/2026/1103/c12379a333598/page.htm"><div class="cai_menu">
            <div class="cai_date">11月03日</div><div class="cai_title line1">[代理比选公告]大规模规划优化求解平台HFWC2026093</div>
            <div class="cai_dep">采购与招投标管理中心</div></div></a></li>""")
        self.assertEqual((title, day), ("[代理比选公告]大规模规划优化求解平台HFWC2026093", "2026-11-03"))

    def test_table_row(self):
        _, _, day = self.one("""<table><tr><td><a href="x.htm">某学院服务器采购项目成交公告</a></td>
            <td>2026/11/06</td></tr></table>""")
        self.assertEqual(day, "2026-11-06")

    def test_skips_navigation_news_and_rules(self):
        html = """<ul>
            <li><a href="/cggg/list.htm">采购公告</a> 2026-11-01</li>
            <li><span>2026-11-03</span><a href="/a.htm">关于进一步优化试剂耗材采购平台自购备案的通知</a></li>
            <li><span>2026-10-30</span><a href="/b.htm">某大学采购与招标管理中心来我校调研交流</a></li>
            <li><span>2026-10-29</span><a href="/c.htm">某大学采购管理办法（2026年修订）</a></li>
            <li><a href="/d.htm">没有日期的某设备采购公告</a></li>
            <li><span>2026-11-09</span><a href="/e.htm">分布式存储服务器竞价公告</a></li></ul>"""
        self.assertEqual([t for t, _, _ in edu.parse_list(html, BASE)], ["分布式存储服务器竞价公告"])

    def test_one_date_far_away_is_not_borrowed(self):
        # 整个列表只有页脚一个日期时，不能把它当成每条公告的日期
        html = "<div>" + "".join(f'<p><a href="/{i}.htm">第{i}号实验设备采购公告</a></p>' for i in range(8)) + \
               "<span>更新于 2026-11-11</span></div>"
        self.assertEqual(edu.parse_list(html, BASE), [])


class FakeResponse:
    def json(self):
        import json
        return json.loads(self.text)

    def __init__(self, text, status=200):
        self.text, self.status, self.encoding, self.apparent_encoding = text, status, "utf-8", "utf-8"

    def raise_for_status(self):
        if self.status >= 400:
            raise RuntimeError(f"HTTP {self.status}")


class FakeSession:
    interval = 0
    headers: dict = {}

    def __init__(self, pages):
        self.pages = pages
        self.urls = []
        self.posts = []

    def get(self, url, **kw):
        self.urls.append(url)
        return self.pages[url]

    def post(self, url, data=None, headers=None):
        self.posts.append(data)
        return self.pages[("POST", url, len(self.posts))]


ROW = '<li><span>{day}</span><a href="{href}">{title}</a></li>'
SCHOOLS = [
    ("aa", "甲大学", ["https://a.edu.cn/list.htm", "https://a.edu.cn/result.htm"]),
    ("bb", "乙学院", ["https://b.edu.cn/list.htm"]),
    ("cc", "丙学院", ["https://c.edu.cn/list.htm"]),
    ("dd", "丁学院", ["https://d.edu.cn/list.htm"]),
]


class FetchTest(unittest.TestCase):
    def pages(self, **override):
        pages = {
            "https://a.edu.cn/list.htm": FakeResponse(
                ROW.format(day="2026-11-10", href="/n/1.htm", title="甲大学学生团体意外保险采购公告")
                + ROW.format(day="2026-09-01", href="/n/0.htm", title="甲大学旧的显微镜采购公告")),
            "https://a.edu.cn/result.htm": FakeResponse(ROW.format(day="2026-11-09", href="/r/1.htm", title="甲大学服务器成交公告")),
            "https://b.edu.cn/list.htm": FakeResponse(ROW.format(day="2026-11-08", href="x.htm?id=5", title="乙学院食堂设备询价公告")),
            "https://c.edu.cn/list.htm": FakeResponse(ROW.format(day="2026-11-07", href="/c1.htm", title="丙学院图书采购公告")),
            "https://d.edu.cn/list.htm": FakeResponse(ROW.format(day="2026-11-06", href="/d1.htm", title="丁学院空调招标公告")),
        }
        pages.update(override)
        return pages

    def setUp(self):
        patcher = mock.patch.object(edu, "PLATFORM_SCHOOLS", [])
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_collects_all_schools(self):
        with mock.patch.object(edu, "SCHOOLS", SCHOOLS):
            notices = edu.fetch(FakeSession(self.pages()), CFG, date(2026, 10, 12))
        self.assertEqual(len(notices), 5)  # 9 月那条在回看窗口之外
        n = notices[0]
        self.assertEqual((n.source, n.source_id, n.purchaser, n.region), ("edu", "aa:/n/1.htm", "甲大学", "上海"))
        self.assertEqual(n.url, "https://a.edu.cn/n/1.htm")
        self.assertIn("bb:/x.htm?id=5", [x.source_id for x in notices])

    def test_one_school_down_does_not_fail_the_rest(self):
        pages = self.pages(**{"https://b.edu.cn/list.htm": FakeResponse("拦截", status=403)})
        with mock.patch.object(edu, "SCHOOLS", SCHOOLS), self.assertLogs(edu.log, "WARNING") as logs:
            notices = edu.fetch(FakeSession(pages), CFG, date(2026, 10, 12))
        self.assertEqual({n.purchaser for n in notices}, {"甲大学", "丙学院", "丁学院"})
        self.assertIn("乙学院", logs.output[0])

    def test_layout_change_counts_as_failure_and_many_failures_raise(self):
        broken = FakeResponse("<html><body>网站升级中</body></html>")
        pages = self.pages(**{"https://b.edu.cn/list.htm": broken, "https://c.edu.cn/list.htm": broken})
        with mock.patch.object(edu, "SCHOOLS", SCHOOLS), self.assertLogs(edu.log, "WARNING"):
            with self.assertRaises(RuntimeError):
                edu.fetch(FakeSession(pages), CFG, date(2026, 10, 12))

    def test_platform_school(self):
        import json
        base = "https://cg.example.edu.cn"
        rows1 = [
            {"id": "a1", "subject": "[招设2026A1]三维表面轮廓仪采购公告", "beginTime": "2026-11-09 10:00:00", "syncTime": "2026-11-09 10:00:05",
             "type": "ZCXQ", "shopTypeName": "公开招标", "url": "https://ebid.example.edu.cn/ZBGG?ggid=9289", "endTime": "2026-11-18 16:00:00"},
            {"id": "a2", "subject": "[招服2026C2]质谱仪维保服务", "beginTime": "2026-11-08 09:00:00", "type": "BYXQ",
             "shopTypeName": "网上比选", "url": None, "syncId": "20MWAEFQ"},
            {"id": "a0", "subject": "很早以前的采购公告", "beginTime": "2026-08-01 09:00:00", "type": "ZCXQ"},
        ]
        rows2 = [{"id": "b1", "subject": "[招设2026C3]锁相放大器网上比选采购评审结果公示", "beginTime": "2026-11-10 09:14:14",
                  "type": "ZCGG", "shopTypeName": "网上比选", "syncId": "20MWBFER"}]
        page_url = base + "/sfw_cms/e?page=cms.psms.gglist&typeDetail=XQ"
        session = FakeSession({
            page_url: FakeResponse('<script>data.ops_uuid="b703917e5499";</script>'),
            ("POST", base + "/sfw_cms/e", 1): FakeResponse(json.dumps({"count": 3, "resultset": rows1})),
            ("POST", base + "/sfw_cms/e", 2): FakeResponse(json.dumps({"count": 1, "resultset": rows2})),
        })
        school = ("pp", "某交通大学", base, "page=cms.psms.gglist&typeDetail=XQ",
                  [[("type", "ZCXQ"), ("ggType", "XQ")], [("type", "ZCGG"), ("ggType", "GG")]])
        with mock.patch.object(edu, "SCHOOLS", []), mock.patch.object(edu, "PLATFORM_SCHOOLS", [school]):
            notices = {n.source_id: n for n in edu.fetch(session, CFG, date(2026, 10, 12))}
        self.assertEqual(sorted(notices), ["pp:a1", "pp:a2", "pp:b1"])
        self.assertEqual(notices["pp:a1"].url, "https://ebid.example.edu.cn/ZBGG?ggid=9289")
        self.assertEqual(notices["pp:a1"].publish_date, "2026-11-09 10:00")
        self.assertEqual(notices["pp:a1"].extra["deadline"], "2026-11-18 16:00")
        self.assertEqual(notices["pp:a2"].url, base + "/provider/#/publish/20MWAEFQ")
        self.assertEqual(notices["pp:a2"].kind, "tender")  # 标题看不出类型，按类型代码 …XQ
        self.assertEqual(notices["pp:b1"].kind, "result")  # 采购方式写「网上比选」，但标题是结果公示
        self.assertIn(("ops_uuid", "b703917e5499"), session.posts[0])
        self.assertIn(("ggType", "GG"), session.posts[1])

    def test_platform_bad_response_is_a_failure(self):
        base = "https://cg.example.edu.cn"
        session = FakeSession({base + "/sfw_cms/e?p": FakeResponse("<html></html>"),
                               ("POST", base + "/sfw_cms/e", 1): FakeResponse('{"error": "session"}')})
        with self.assertRaises(RuntimeError):
            edu.fetch_platform(session, "pp", "某交通大学", base, "p", [[]])

    def test_school_keys_are_unique(self):
        keys = [k for k, _, _ in edu.SCHOOLS] + [row[0] for row in edu.PLATFORM_SCHOOLS]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertTrue(all(u.startswith("https://") for _, _, urls in edu.SCHOOLS for u in urls))


if __name__ == "__main__":
    unittest.main()
