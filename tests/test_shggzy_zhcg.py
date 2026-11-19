import unittest
from datetime import date

from tender_alert.config import Config
from tender_alert.sources import shggzy_zhcg

CFG = Config(keywords=["保险"], exclude=[], regions=["上海"], kinds=["tender"], lookback_days=7,
             sources=[], request_interval=0, failure_threshold=3)


def list_page(rows, token=""):
    """rows: (编号, 标题, 日期)；编号写成「前缀/编号」可以指定详情页路径，默认是采购公告。"""
    items = "".join(
        f"<li onclick=\"window.open('/{cid if '/' in cid else 'jyxxwzcggg/' + cid}?cExt=&isIndex=')\">"
        f"<span>·</span><span>{title}</span><span>ZHCG{cid}</span><span>{day}</span></li>"
        for cid, title, day in rows
    )
    return f'<input id="cExt" name="cExt" value="{token}"><div id="allList"><ul>{items}</ul></div>'


class FakeResponse:
    def __init__(self, text, url):
        self.text, self.url = text, url

    def raise_for_status(self):
        pass


class FakeSession:
    """按 URL 返回预先准备好的页面，记录请求过哪些 URL。"""

    def __init__(self, pages):
        self.pages, self.requested = pages, []
        self.interval, self.headers = 0, {}

    def get(self, url, params=None, headers=None):
        self.requested.append(url)
        return FakeResponse(self.pages[url], url)


HOME = list_page([("100", "首页", "2026-10-30")], token="TOK")
EMPTY_DYLY = list_page([("900", "无关单一来源公示", "2026-10-01")])  # 早于回看范围，翻 1 页就停


class CleanTitleTest(unittest.TestCase):
    def test_removes_repeated_project_name(self):
        self.assertEqual(
            shggzy_zhcg.clean_title("上海磁浮2027年度员工综合福利保险项目-上海磁浮2027年度员工综合福利保险项目采购公告"),
            "上海磁浮2027年度员工综合福利保险项目采购公告")

    def test_project_name_with_dash(self):
        self.assertEqual(
            shggzy_zhcg.clean_title("车辆保险服务2026-2029年度采购项目-车辆保险服务2026-2029年度采购项目采购公告"),
            "车辆保险服务2026-2029年度采购项目采购公告")

    def test_whitespace_in_list_title_is_collapsed(self):
        rows = shggzy_zhcg._rows(list_page([("1", "银行董监高责任险项目-银行董监高责任险项目\t竞争性磋商成交结果公告", "2026-10-30")]))
        self.assertEqual(shggzy_zhcg.clean_title(rows[0][1]), "银行董监高责任险项目 竞争性磋商成交结果公告")

    def test_inconsistent_spaces_in_halves(self):
        self.assertEqual(
            shggzy_zhcg.clean_title("马陆镇安置房 --工程质量潜在缺陷保险服务-马陆镇安置房--工程质量潜在缺陷保险服务成交结果公告"),
            "马陆镇安置房--工程质量潜在缺陷保险服务成交结果公告")

    def test_other_titles_unchanged(self):
        self.assertEqual(shggzy_zhcg.clean_title("A项目-B项目采购公告"), "A项目-B项目采购公告")


class FetchTest(unittest.TestCase):
    def test_stops_at_last_seen_and_remembers_newest(self):
        session = FakeSession({
            "https://www.shggzy.com/jyxxwzcg": HOME,
            "https://www.shggzy.com/jyxxwzcggg": list_page([
                ("5", "X-X车辆保险采购公告", "2026-10-30"), ("4", "食堂采购公告", "2026-10-30")]),
            "https://www.shggzy.com/jyxxwzcggg_2.jhtml": list_page([
                ("3", "旧的采购公告", "2026-10-29"), ("2", "更旧的采购公告", "2026-10-29")]),
            "https://www.shggzy.com/jyxxwzcgdyly": EMPTY_DYLY,
        })
        state = {"backfilled": "2026-10-29", "jyxxwzcggg": ["3", "2"], "jyxxwzcgdyly": ["900"]}
        notices = shggzy_zhcg.fetch(session, CFG, date(2026, 10, 23), state)
        self.assertNotIn("https://www.shggzy.com/jyxxwzcggg_3.jhtml", session.requested)
        self.assertEqual(state["jyxxwzcggg"][:4], ["5", "4", "3", "2"])
        n = next(n for n in notices if n.source_id == "5")
        self.assertEqual(n.title, "X车辆保险采购公告")
        self.assertEqual(n.url, "https://www.shggzy.com/jyxxwzcggg/5?cExt=TOK&isIndex=")
        self.assertEqual(n.kind, "tender")

    def test_first_run_stops_when_older_than_window(self):
        session = FakeSession({
            "https://www.shggzy.com/jyxxwzcg": HOME,
            "https://www.shggzy.com/jyxxwzcggg": list_page([("5", "a", "2026-10-30")]),
            "https://www.shggzy.com/jyxxwzcggg_2.jhtml": list_page([("4", "b", "2026-10-20")]),
            "https://www.shggzy.com/jyxxwzcgdyly": EMPTY_DYLY,
        })
        shggzy_zhcg.fetch(session, CFG, date(2026, 10, 23), {}, dry_run=True)
        self.assertNotIn("https://www.shggzy.com/jyxxwzcggg_3.jhtml", session.requested)

    def test_first_real_run_backfills_with_search_once(self):
        search = (
            "<script>var showCount = 2;</script>" + list_page([
                ("7", "车辆<font color='red'>保险</font>项目-车辆<font color='red'>保险</font>项目采购公告", "2026-10-27"),
                ("jyxxwzcgcgyx/8", "职工补充医疗<font color='red'>保险</font>项目-职工补充医疗<font color='red'>保险</font>项目采购意向", "2026-10-05"),
            ])
        )
        pages = {
            "https://www.shggzy.com/jyxxwzcg": HOME,
            "https://www.shggzy.com/search/queryContents.jhtml": search,
            "https://www.shggzy.com/jyxxwzcggg": list_page([("5", "a", "2026-10-30")]),
            "https://www.shggzy.com/jyxxwzcgdyly": EMPTY_DYLY,
        }
        state = {}
        notices = shggzy_zhcg.fetch(FakeSession(pages), CFG, date(2026, 9, 30), state)
        by_id = {n.source_id: n for n in notices}
        self.assertEqual(by_id["7"].title, "车辆保险项目采购公告")
        self.assertEqual(by_id["7"].kind, "tender")
        self.assertEqual(by_id["8"].kind, "other")  # 采购意向的路径前缀不认识，交给 classify_kind 按标题判断
        self.assertEqual(state["backfilled"], date.today().isoformat())
        self.assertEqual(state["jyxxwzcggg"], ["5"])

        # 第二次运行不再搜索；第 1 页已经包含上次看过的那条，所以只看第 1 页
        session = FakeSession(pages)
        shggzy_zhcg.fetch(session, CFG, date(2026, 9, 30), state)
        self.assertNotIn("https://www.shggzy.com/search/queryContents.jhtml", session.requested)

    def test_dry_run_never_searches(self):
        session = FakeSession({
            "https://www.shggzy.com/jyxxwzcg": HOME,
            "https://www.shggzy.com/jyxxwzcggg": list_page([("5", "a", "2026-10-01")]),
            "https://www.shggzy.com/jyxxwzcgdyly": EMPTY_DYLY,
        })
        state = {}
        shggzy_zhcg.fetch(session, CFG, date(2026, 10, 23), state, dry_run=True)
        self.assertNotIn("https://www.shggzy.com/search/queryContents.jhtml", session.requested)
        self.assertNotIn("backfilled", state)

    def test_slider_page_raises(self):
        session = FakeSession({"https://www.shggzy.com/jyxxwzcg": "<title>访问验证</title>图片滑动验证"})
        with self.assertRaises(RuntimeError):
            shggzy_zhcg.fetch(session, CFG, date(2026, 10, 23), {})

    def test_missing_token_raises(self):
        session = FakeSession({"https://www.shggzy.com/jyxxwzcg": list_page([("1", "a", "2026-10-30")])})
        with self.assertRaises(RuntimeError):
            shggzy_zhcg.fetch(session, CFG, date(2026, 10, 23), {})


if __name__ == "__main__":
    unittest.main()
