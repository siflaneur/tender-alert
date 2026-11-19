import unittest
from datetime import date, datetime
from unittest import mock

from tender_alert.config import Config
from tender_alert.sources import shmetro

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
OPEN_TIME = datetime(2026, 11, 2, 10, 0)  # 平台开放时间内


def pane(tab_id, pane_id, label, rows, container_id):
    trs = "".join(
        f'<tr><td class="newstdTJ"></td>'
        f'<td class="newstd1"><a target="_blank">{title}</a></td>'
        f'<td class="newstd2">[{day}]</td><td class="newstd3">[3次]</td></tr>'
        for title, day in rows
    )
    return (
        '<div class="el-tabs">'
        '<div class="el-tabs__header"><div class="el-tabs__nav-wrap"><div class="el-tabs__nav">'
        f'<div id="{tab_id}" aria-controls="{pane_id}" class="el-tabs__item">{label}</div>'
        "</div></div></div>"
        '<div class="el-tabs__content">'
        f'<div id="{pane_id}" class="el-tab-pane">'
        f'<div id="{container_id}" class="tagContent selectTag"><ul><table>{trs}</table></ul></div>'
        "</div></div></div>"
    )


def page(*panes):
    return "<html><body>" + "".join(panes) + "</body></html>"


class ShmetroParseTest(unittest.TestCase):
    def test_parses_tabs_and_maps_kinds(self):
        html = page(
            pane("tab-0", "pane-0", "招标公告", [
                ("2026年新增焊缝探伤项目招标公告", "20261030"),
                ("3号线北延伸电缆更换项目招标公告", "20261030"),
                ("2027-2029年度上海地铁运营线路保险项目变更公告", "20261117"),
                ("2027-2029年度运营财产险保险顾问咨询服务项目延期公告", "20261109"),
            ], "tagContentMj0"),
            pane("tab-1", "pane-1", "单一来源公示", [
                ("供电分公司应急三相继保箱物资采购项目单一来源", "20261029"),
            ], "tagContentMj2"),
            pane("tab-2", "pane-2", "中标结果公告", [
                ("某项目中标结果公告", "20261028"),
            ], "tagContentMj1"),
        )

        notices = shmetro._parse(html)

        self.assertEqual(len(notices), 6)
        by_title = {n.title: n for n in notices}
        tender = by_title["2026年新增焊缝探伤项目招标公告"]
        self.assertEqual(tender.source, "shmetro")
        self.assertEqual(tender.publish_date, "2026-10-30")
        self.assertEqual(tender.notice_type, "招标公告")
        self.assertEqual(tender.kind, "tender")
        self.assertEqual(by_title["2027-2029年度上海地铁运营线路保险项目变更公告"].kind, "correction")
        self.assertEqual(by_title["2027-2029年度运营财产险保险顾问咨询服务项目延期公告"].kind, "correction")
        self.assertEqual(tender.region, "上海")
        self.assertEqual(tender.url, shmetro.HOME)
        self.assertEqual(len(tender.source_id), 16)
        self.assertEqual(by_title["供电分公司应急三相继保箱物资采购项目单一来源"].kind, "tender")
        self.assertEqual(by_title["某项目中标结果公告"].kind, "result")
        self.assertEqual(by_title["某项目中标结果公告"].publish_date, "2026-10-28")

    def test_same_title_and_date_is_one_notice(self):
        html = page(
            pane("tab-0", "pane-0", "招标公告",
                 [("重复招标公告", "20261030")], "tagContentMj0"),
            pane("tab-1", "pane-1", "揭榜询比采购公告",
                 [("重复招标公告", "20261030")], "openBiddingContent0"),
        )
        notices = shmetro._parse(html)
        self.assertEqual(len(notices), 1)

    def test_bad_rows_are_skipped(self):
        html = page(pane("tab-0", "pane-0", "招标公告", [
            ("正常招标公告", "20261030"),
            ("没有日期的公告", ""),
        ], "tagContentMj0"))
        notices = shmetro._parse(html)
        self.assertEqual([n.title for n in notices], ["正常招标公告"])

    def test_layout_change_raises(self):
        with self.assertRaises(RuntimeError):
            shmetro._parse("<html><body>网站改版了</body></html>")

    def test_fetch_renders_page(self):
        html = page(pane("tab-0", "pane-0", "招标公告",
                         [("员工补充医疗保险采购公告", "20261030")], "tagContentMj0"))
        with mock.patch.object(shmetro, "_now", return_value=OPEN_TIME), \
                mock.patch.object(shmetro, "_render", return_value=html):
            notices = shmetro.fetch(object(), CFG, SINCE)
        self.assertEqual([n.title for n in notices], ["员工补充医疗保险采购公告"])

    def test_render_failure_raises(self):
        with mock.patch.object(shmetro, "_now", return_value=OPEN_TIME), \
                mock.patch.object(shmetro, "_render", side_effect=RuntimeError("找不到 Chrome")):
            with self.assertRaises(RuntimeError):
                shmetro.fetch(object(), CFG, SINCE)

    def test_outside_opening_hours_skips_without_failing(self):
        for hour in (6, 19, 23):
            with mock.patch.object(shmetro, "_now", return_value=datetime(2026, 11, 2, hour, 30)), \
                    mock.patch.object(shmetro, "_render", side_effect=AssertionError("不该打开页面")):
                self.assertEqual(shmetro.fetch(object(), CFG, SINCE), [], hour)


if __name__ == "__main__":
    unittest.main()
