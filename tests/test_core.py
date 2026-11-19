import smtplib
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from tender_alert.config import Config, MailSettings
from tender_alert.filters import in_region, in_window, match, still_matches
from tender_alert.mailer import build_html, send
from tender_alert.models import Notice, classify_kind
from tender_alert.sources import ccgp
from tender_alert.store import Store, normalize_title

CFG = Config(
    keywords=["保险", "车险", "意外险", "责任险", "财产险", "补充医疗", "承保"],
    exclude=["社会保险缴纳", "社会保险费", "社保代缴", "保险柜", "保险箱", "保险丝", "保险杠"],
    regions=["上海"],
    kinds=["tender", "correction", "other"],
    lookback_days=7,
    sources=[],
    request_interval=0,
    failure_threshold=3,
)


def notice(title, source="zfcg_sh", sid="1", day="2026-10-28", **kw):
    n = Notice(source=source, source_id=sid, title=title, url="https://example.com/" + sid, publish_date=day, **kw)
    n.kind = classify_kind(n.notice_type, n.title)
    return n


class ClassifyKindTest(unittest.TestCase):
    def test_kinds(self):
        cases = {
            "上海市某区教育局2026年车辆保险服务公开招标公告": "tender",
            "某医院团体意外险采购项目竞争性磋商公告": "tender",
            "某单位财产保险服务项目更正公告": "correction",
            "某单位财产保险服务项目招标公告（更正）": "correction",
            "某单位车辆保险项目中标结果公告": "result",
            "某单位2027年度保险服务采购意向": "intent",
            "某单位车辆保险服务项目": "other",
        }
        for title, kind in cases.items():
            self.assertEqual(classify_kind(None, title), kind, title)

    def test_notice_type_wins_over_title(self):
        self.assertEqual(classify_kind("中标（成交）结果公告", "某单位车辆保险服务项目"), "result")

    def test_vague_notice_type_falls_back_to_title(self):
        # 中国政府采购网把废标公告归在「其他公告」里
        self.assertEqual(classify_kind("其他公告", "上海科技大学2027外籍职工补充医疗保险的废标公告"), "result")
        self.assertEqual(classify_kind("采购终止（废标）公告", "某项目的废标公告"), "result")
        self.assertEqual(classify_kind(None, "中国浦东干部学院2026年保险服务采购项目的失败公告"), "result")
        self.assertEqual(classify_kind(None, "某医院门窗维修服务比选谈判失败公告（二次）"), "result")
        self.assertEqual(classify_kind("单一来源采购公示", "某项目单一来源采购公示"), "tender")


class MatchTest(unittest.TestCase):
    def test_keyword_hit(self):
        self.assertEqual(match(notice("某局车辆保险服务招标公告"), CFG, True), ["保险"])

    def test_exclude_word(self):
        self.assertIsNone(match(notice("某档案馆保险柜采购公告"), CFG, True))
        self.assertIsNone(match(notice("某单位社保代缴服务采购公告"), CFG, True))

    def test_no_keyword(self):
        self.assertIsNone(match(notice("某学校食堂食材采购公告"), CFG, True))

    def test_insurer_as_buyer_is_noise(self):
        self.assertIsNone(match(notice("中国大地财产保险股份有限公司上海分公司硬件项目招标公告"), CFG, True))
        self.assertIsNone(match(notice("新华人寿保险股份有限公司办公家具采购公告"), CFG, True))
        self.assertIsNone(match(notice("上海市医疗保险事务中心硒鼓采购公告"), CFG, True))

    def test_insurance_service_titles_are_not_mistaken_for_agencies(self):
        for title in ("某区学生团体保险管理及理赔处理服务采购项目", "某区机关车辆保险管理与理赔处置服务采购项目"):
            self.assertEqual(match(notice(title), CFG, True), ["保险"], title)

    def test_still_matches_follows_current_config(self):
        row = {"kind": "correction", "title": "某局车辆保险服务更正公告", "keywords": "保险"}
        self.assertTrue(still_matches(row, CFG))
        self.assertFalse(still_matches(row, Config(**{**CFG.__dict__, "kinds": ["tender"]})))
        self.assertFalse(still_matches(row, Config(**{**CFG.__dict__, "exclude": ["车辆保险"]})))
        self.assertFalse(still_matches(row, Config(**{**CFG.__dict__, "keywords": ["车险"]})))

    def test_insurer_name_does_not_hide_real_keyword(self):
        n = notice("某集团车辆保险采购（中国人寿财产保险股份有限公司）招标公告")
        self.assertEqual(match(n, CFG, True), ["保险"])
        self.assertEqual(match(notice("某区遴选保险公司项目招标公告"), CFG, True), ["保险"])

    def test_match_text_for_intent(self):
        cfg = Config(**{**CFG.__dict__, "kinds": ["intent"]})
        n = notice("上海市民政局2027年08月政府采购意向", match_text="采购项目名称 为老服务机构综合责任保险 预算 50万元")
        self.assertEqual(match(n, cfg, True), ["保险"])
        self.assertIsNone(match(notice("上海市民政局2027年08月政府采购意向"), cfg, True))

    def test_result_excluded_by_default(self):
        self.assertIsNone(match(notice("某单位车辆保险项目中标结果公告"), CFG, True))

    def test_region_for_national_source(self):
        far = notice("北京某单位车辆保险服务招标公告", source="ccgp", region="北京")
        near = notice("某中央单位车辆保险服务招标公告", source="ccgp", purchaser="某中央单位上海分局")
        self.assertIsNone(match(far, CFG, False))
        self.assertEqual(match(near, CFG, False), ["保险"])

    def test_window(self):
        today = date(2026, 10, 29)
        self.assertTrue(in_window(notice("x", day="2026-10-22"), CFG, today))
        self.assertFalse(in_window(notice("x", day="2026-10-21"), CFG, today))
        self.assertTrue(in_window(notice("x", day="未知"), CFG, today))


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "t.db")

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def test_dedup_same_source(self):
        n = notice("某局车辆保险服务招标公告")
        self.assertTrue(self.store.add(n, ["保险"]))
        self.assertFalse(self.store.add(n, ["保险"]))
        self.assertEqual(len(self.store.pending("2026-10-01")), 1)

    def test_dedup_across_sources(self):
        self.assertTrue(self.store.add(notice("某局车辆保险服务招标公告", source="zfcg_sh", sid="a"), ["保险"]))
        # 跨站转载记录为重复后返回 False，避免日志虚报「新命中」
        self.assertFalse(self.store.add(notice("某局 车辆保险服务招标公告。", source="ccgp", sid="b", day="2026-10-29"), ["保险"]))
        self.assertEqual([r["source"] for r in self.store.pending("2026-10-01")], ["zfcg_sh"])

    def test_dedup_when_reposter_rewrites_type_suffix(self):
        # 2026-11-03 申通地铁和综合采购同一条，标题只差后缀，之前被提醒了两次
        self.store.add(notice("2026-2028年上海地铁维护保障有限公司职工商业保险项目采购公告",
                              source="shggzy_zhcg", sid="a", day="2026-11-03"), ["商业保险"])
        self.store.add(notice("2026-2028年上海地铁维护保障有限公司职工商业保险项目招标公告",
                              source="shmetro", sid="b", day="2026-11-03"), ["商业保险"])
        self.assertEqual([r["source"] for r in self.store.pending("2026-10-01")], ["shggzy_zhcg"])
        self.store.add(notice("某局车辆保险项目中标结果公告", source="shmetro", sid="c"), ["保险"])
        self.store.add(notice("某局车辆保险项目成交结果公告", source="shggzy_zhcg", sid="d"), ["保险"])
        self.assertEqual(len(self.store.pending("2026-10-01")), 2)

    def test_tender_and_result_of_same_project_both_kept(self):
        self.store.add(notice("某局车辆保险服务项目招标公告", sid="a", day="2026-10-27"), ["保险"])
        self.store.add(notice("某局车辆保险服务项目中标结果公告", sid="b", day="2026-10-29"), ["保险"])
        self.assertEqual(len(self.store.pending("2026-10-01")), 2)

    def test_different_events_not_merged(self):
        # 真实存档里差点被误合并的几种（2026-11-04 用全量存档压力测试发现）
        pairs = [
            ("ceair", "手持智能终端车载保险", "ceair", "手持智能终端车载保险（终止）"),
            ("baowu", "梅钢能环部车辆保险服务", "baowu", "梅钢能环部车辆保险服务中标结果公告"),
            ("zfcg_sh", "购买第三方车辆保险服务的废标公告", "zfcg_sh", "购买第三方车辆保险服务的中标（成交）结果公告"),
            ("zfcg_sh", "某局车辆保险服务项目中标候选人公示", "zfcg_sh", "某局车辆保险服务项目中标结果公告"),
            ("zfcg_sh", "某中学车辆保险服务的合同公告", "zfcg_sh", "某中学车辆保险服务的合同"),
        ]
        for i, (s1, t1, s2, t2) in enumerate(pairs):
            self.store.add(notice(t1, source=s1, sid=f"{i}a"), ["保险"])
            self.store.add(notice(t2, source=s2, sid=f"{i}b"), ["保险"])
        self.assertEqual(len(self.store.pending("2026-10-01")), 2 * len(pairs))

    def test_retender_is_not_duplicate(self):
        self.store.add(notice("某局车辆保险服务项目招标公告", sid="a", day="2026-10-27"), ["保险"])
        self.store.add(notice("某局车辆保险服务项目重新招标公告", sid="b", day="2026-10-29"), ["保险"])
        self.assertEqual(len(self.store.pending("2026-10-01")), 2)

    def test_normalize_title(self):
        self.assertEqual(normalize_title("某区教育局空调设备采购项目招标公告"), normalize_title("某区教育局 空调设备采购公告"))
        self.assertEqual(normalize_title("采购公告"), "采购公告")  # 去掉后缀就没了，保留原样

    def test_existing_rows_renormalized_on_open(self):
        self.store.add(notice("某局车辆保险项目采购公告", sid="a"), ["保险"])
        self.store.db.execute("UPDATE notices SET norm_title = '旧规则的值'")
        self.store.db.commit()
        reopened = Store(Path(self.tmp.name) / "t.db")
        self.assertEqual(reopened.db.execute("SELECT norm_title FROM notices").fetchone()[0],
                         normalize_title("某局车辆保险项目采购公告"))
        reopened.db.close()

    def test_same_title_same_source_different_ids(self):
        for sid in ("a", "b", "c"):
            self.store.add(notice("2026-2027年度飞机维修人责任险", source="ceair", sid=sid, day="2026-10-19"), ["责任险"])
        self.assertEqual(len(self.store.pending("2026-10-01")), 1)

    def test_same_title_far_apart_is_not_duplicate(self):
        self.store.add(notice("某局车辆保险服务招标公告", source="zfcg_sh", sid="a", day="2026-10-01"), ["保险"])
        self.store.add(notice("某局车辆保险服务招标公告", source="ccgp", sid="b", day="2026-10-28"), ["保险"])
        self.assertEqual(len(self.store.pending("2026-09-01")), 2)

    def test_mark_notified(self):
        self.store.add(notice("某局车辆保险服务招标公告"), ["保险"])
        self.store.mark_notified(self.store.pending("2026-10-01"))
        self.assertEqual(self.store.pending("2026-10-01"), [])

    def test_failure_counter(self):
        self.assertEqual(self.store.record_failure("ccgp", "boom"), 1)
        self.assertEqual(self.store.record_failure("ccgp", "boom"), 2)
        self.store.record_success("ccgp")
        self.assertEqual(self.store.record_failure("ccgp", "boom"), 1)

    def test_alert_persists_until_sent(self):
        for _ in range(3):
            self.store.record_failure("ccgp", "boom")
        self.assertEqual([r["source"] for r in self.store.unalerted_failures(3)], ["ccgp"])
        # 第 4 次失败时告警还没发出去（比如那次是 dry-run），仍然要告警
        self.store.record_failure("ccgp", "boom")
        self.assertEqual(len(self.store.unalerted_failures(3)), 1)
        self.store.mark_alerted(["ccgp"])
        self.store.record_failure("ccgp", "boom")
        self.assertEqual(self.store.unalerted_failures(3), [])
        # 恢复后再次坏掉，要重新告警
        self.store.record_success("ccgp")
        for _ in range(3):
            self.store.record_failure("ccgp", "boom")
        self.assertEqual(len(self.store.unalerted_failures(3)), 1)

    def test_undated_notice_is_still_pending(self):
        self.store.add(notice("某局车辆保险服务招标公告", day=""), ["保险"])
        self.assertEqual(len(self.store.pending("2026-10-22")), 1)

    def test_higher_priority_source_becomes_primary(self):
        prio = ["zfcg_sh", "shggzy", "ccgp"]
        self.store.add(notice("某局车辆保险服务招标公告", source="ccgp", sid="b"), ["保险"], prio)
        self.store.add(notice("某局车辆保险服务招标公告", source="zfcg_sh", sid="a"), ["保险"], prio)
        self.assertEqual([r["source"] for r in self.store.pending("2026-10-01")], ["zfcg_sh"])

    def test_notified_primary_is_never_replaced(self):
        prio = ["zfcg_sh", "shggzy", "ccgp"]
        self.store.add(notice("某局车辆保险服务招标公告", source="ccgp", sid="b"), ["保险"], prio)
        self.store.mark_notified(self.store.pending("2026-10-01"))
        self.store.add(notice("某局车辆保险服务招标公告", source="zfcg_sh", sid="a"), ["保险"], prio)
        self.assertEqual(self.store.pending("2026-10-01"), [])

    def test_scratch_store_does_not_touch_disk(self):
        path = Path(self.tmp.name) / "t.db"
        self.store.add(notice("某局车辆保险服务招标公告", sid="a"), ["保险"])
        scratch = Store(path, scratch=True)
        scratch.add(notice("某局财产保险服务招标公告", sid="b"), ["保险"])
        scratch.record_failure("ccgp", "boom")
        self.assertEqual(len(scratch.pending("2026-10-01")), 2)
        scratch.db.close()
        self.assertEqual(len(self.store.pending("2026-10-01")), 1)
        self.assertEqual(self.store.unalerted_failures(1), [])


    def test_archive_new_then_update(self):
        n = notice("某学校食堂食材采购公告", source="zfcg_sh", sid="x")
        self.assertTrue(self.store.archive(n))
        # 内容完全没变时不刷新 last_seen，避免历史月份 CSV 每次运行都被重写
        self.store.db.execute("UPDATE archive SET first_seen = '2026-01-01T00:00:00', last_seen = '2026-01-01T00:00:00'")
        self.store.db.commit()
        self.assertFalse(self.store.archive(n))
        unchanged_seen = self.store.db.execute("SELECT last_seen FROM archive").fetchone()[0]
        self.assertEqual(unchanged_seen, "2026-01-01T00:00:00")
        # 内容发生变化时才更新字段并刷新 last_seen
        n.title = "某学校食堂食材采购公告（更正）"
        self.assertFalse(self.store.archive(n))
        row = self.store.db.execute("SELECT title, last_seen > '2026-01-01T00:00:00' FROM archive").fetchone()
        self.assertEqual(tuple(row), ("某学校食堂食材采购公告（更正）", 1))
        self.assertEqual(self.store.pending("2026-10-01"), [])  # 存档不影响保险提醒

    def test_batch_commits_atomically(self):
        with self.store.batch():
            self.store.archive(notice("批量存档1", sid="1"))
            self.store.archive(notice("批量存档2", sid="2"))
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM archive").fetchone()[0], 2)


class ArchiveConfigTest(unittest.TestCase):
    def test_fetch_kinds_follows_archive_switch(self):
        self.assertEqual(CFG.fetch_kinds, CFG.kinds)
        on = Config(**{**CFG.__dict__, "archive": True})
        self.assertEqual(set(on.fetch_kinds), {"tender", "correction", "result", "intent", "other"})

    def test_in_region(self):
        self.assertTrue(in_region(notice("某工程", region="北京"), CFG, True))
        self.assertTrue(in_region(notice("某工程", purchaser="上海某局"), CFG, False))
        self.assertFalse(in_region(notice("某工程", region="北京"), CFG, False))

MAIL = MailSettings(host="smtp.example.com", port=465, user="a@example.com", password="x", to=["b@example.com"])


class ReportMailTest(unittest.TestCase):
    def test_report_without_new_notices(self):
        report = [
            {"name": "上海政府采购网", "fetched": 944, "new": 0, "archived": 944},
            {"name": "国泰海通·供应商门户", "error": "SSLError: handshake failed", "failures": 2},
        ]
        html = build_html([], [], {}, report)
        self.assertIn("本次没有新的上海保险类公告", html)
        self.assertIn("2 个来源，1 个正常、1 个失败，共新存档 944 条", html)
        self.assertIn("失败（连续第 2 次）：SSLError: handshake failed", html)

    def test_report_shows_sources_skipped_today(self):
        report = [{"name": "上海政府采购网", "fetched": 900, "new": 0, "archived": 300},
                  {"name": "上海高校·采购招标公告", "skipped": True}]
        html = build_html([], [], {}, report)
        self.assertIn("2 个来源，1 个正常、1 个今天已抓过", html)
        # 正常和跳过的来源不逐个列出，邮件里不出现它们的名字
        self.assertNotIn("上海政府采购网", html)
        self.assertNotIn("上海高校·采购招标公告", html)

    def test_report_without_archive_hides_archive_column(self):
        html = build_html([], [], {}, [{"name": "上海银行", "fetched": 14, "new": 0, "archived": None}])
        self.assertNotIn("新存档", html)

    def test_notice_detail_line_matches_prior_font_size(self):
        row = {
            "publish_date": "2026-11-18",
            "kind": "tender",
            "url": "https://example.com/1",
            "title": "员工团体保险采购项目招标公告",
            "purchaser": "测试单位",
            "source": "orgs",
            "keywords": "保险",
            "extra": '{"deadline": "2099-11-23", "prior": {"status": "none"}}',
        }
        html = build_html([row], [], {"orgs": "企事业单位"})
        self.assertIn('color:#888;font-size:12px">往年核查：近 3 年公开渠道未见同标</span>', html)
        self.assertIn('color:#888;font-size:12px">截止/开标 2099-11-23 · 命中：保险</span>', html)
        self.assertIn("由 tender-alert 自动发送。</p>", html)
        self.assertNotIn("config.toml", html)


class SendTest(unittest.TestCase):
    def test_bad_quit_reply_after_delivery_is_not_a_failure(self):
        with mock.patch("smtplib.SMTP_SSL") as smtp:
            smtp.return_value.quit.side_effect = smtplib.SMTPResponseException(-1, b"\x00\x00\x00")
            send(MAIL, "s", "<p>x</p>")
            smtp.return_value.sendmail.assert_called_once()
            smtp.return_value.close.assert_called_once()

    def test_login_failure_raises(self):
        with mock.patch("smtplib.SMTP_SSL") as smtp:
            smtp.return_value.login.side_effect = smtplib.SMTPAuthenticationError(535, b"bad")
            with self.assertRaises(smtplib.SMTPAuthenticationError):
                send(MAIL, "s", "<p>x</p>")
            smtp.return_value.sendmail.assert_not_called()


CCGP_ITEM = (
    '<li><a href="http://www.ccgp.gov.cn/cggg/zygg/gkzb/202610/t20261009_27448388.htm">'
    '中国浦东干部学院2026年<font color="red">保险</font>服务采购项目公开招标公告</a><p>…</p>'
    "<span>{time} | 采购人：中国浦东干部学院 | 代理机构：某代理 <br/> <strong>公开招标公告</strong>"
    " | 上海 | <strong>服务/保险服务</strong></span></li>"
)


def ccgp_page(total, items, ul=True):
    body = f'<ul class="vT-srch-result-list-bid">{items}</ul>' if ul else "<div>改版了</div>"
    return f"<p>共找到<span>{total}</span>条</p>{body}<script>Pager({{size:1, current:0}})</script>"


class CcgpParseTest(unittest.TestCase):
    def test_normal_page(self):
        notices, pages = ccgp._parse(ccgp_page(1, CCGP_ITEM.format(time="2026.10.09 10:23:17")))
        self.assertEqual(pages, 1)
        n = notices[0]
        self.assertEqual(n.title, "中国浦东干部学院2026年保险服务采购项目公开招标公告")
        self.assertEqual(n.source_id, "27448388")
        self.assertEqual(n.publish_date, "2026-10-09 10:23")
        self.assertEqual(n.purchaser, "中国浦东干部学院")
        self.assertEqual(n.notice_type, "公开招标公告")
        self.assertEqual(n.region, "上海")

    def test_zero_results(self):
        self.assertEqual(ccgp._parse(ccgp_page(0, "")), ([], 1))

    def test_layout_changes_raise_instead_of_returning_nothing(self):
        with self.assertRaises(RuntimeError):
            ccgp._parse(ccgp_page(16, "", ul=False))
        with self.assertRaises(RuntimeError):
            ccgp._parse(ccgp_page(16, ""))
        with self.assertRaises(RuntimeError):
            ccgp._parse(ccgp_page(1, CCGP_ITEM.format(time="2026-10-09 10:23:17")))
        with self.assertRaises(RuntimeError):
            ccgp._parse("<html>没有结果数</html>")

    def test_rate_limited(self):
        with self.assertRaises(ccgp.RateLimited):
            ccgp._parse("<title>频繁访问!</title>您的访问过于频繁,请稍后再试。")




class OnceADayTest(unittest.TestCase):
    """更新慢的来源每天只抓一次。"""

    def setUp(self):
        from argparse import Namespace
        from dataclasses import replace
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "t.db")
        self.cfg = replace(CFG, once_a_day=("edu",))
        self.args = Namespace(sources=None, record=False, dry_run=False)
        self.today = date.today()

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def done(self, key, **kw):
        from argparse import Namespace
        from tender_alert.__main__ import already_done_today
        args = Namespace(**{**vars(self.args), **kw})
        return already_done_today(key, self.cfg, self.store, args, self.today)

    def test_skips_only_after_success_today(self):
        self.assertFalse(self.done("edu"))  # 今天还没抓过
        self.store.record_failure("edu", "boom")
        self.assertFalse(self.done("edu"))  # 失败不算，下次运行补抓
        self.store.record_success("edu")
        self.assertTrue(self.done("edu"))

    def test_yesterdays_success_does_not_count(self):
        self.store.record_success("edu")
        self.store.db.execute("UPDATE source_health SET last_success = '2020-01-01T10:00:00'")
        self.assertFalse(self.done("edu"))

    def test_frequent_sources_and_explicit_requests_always_run(self):
        self.store.record_success("edu")
        self.store.record_success("zfcg_sh")
        self.assertFalse(self.done("zfcg_sh"))  # 不在每天一次的名单里
        self.assertFalse(self.done("edu", sources=["edu"]))  # 用 --sources 点名
        self.assertFalse(self.done("edu", record=True))  # 录样本


class RuntimeGuardTest(unittest.TestCase):
    def test_single_instance_lock_blocks_second_holder_then_releases(self):
        from tender_alert.__main__ import single_instance_lock
        with tempfile.TemporaryDirectory() as tmp:
            lock_file = Path(tmp) / "run.lock"
            with single_instance_lock(lock_file) as first:
                self.assertTrue(first)
                with single_instance_lock(lock_file) as second:
                    self.assertFalse(second)
            with single_instance_lock(lock_file) as third:
                self.assertTrue(third)

    def test_write_preview_keeps_at_most_max_previews(self):
        from tender_alert.__main__ import MAX_PREVIEWS, write_preview
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            for i in range(MAX_PREVIEWS + 5):
                (out / f"20261118-1000{i:02d}.html").write_text("old", encoding="utf-8")
            latest = write_preview("测试主题", "<p>正文</p>", preview_dir=out)
            remaining = list(out.glob("*.html"))
            self.assertEqual(len(remaining), MAX_PREVIEWS)
            self.assertIn(latest, remaining)


if __name__ == "__main__":
    unittest.main()
