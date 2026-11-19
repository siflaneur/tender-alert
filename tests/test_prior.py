"""往年同标续标核查、工程险剔除与中标公司提取的测试（不联网）。"""

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from tender_alert.config import Config
from tender_alert.export import export_csv
from tender_alert.mailer import build_html
from tender_alert.models import Notice
from tender_alert.prior import (
    Candidate,
    enrich_pending,
    extract_winners,
    format_prior_text,
    is_one_off_project,
    is_same_project,
    lookup_prior,
    pick_best,
)
from tender_alert.store import Store

CFG = Config(
    keywords=["保险", "车险", "意外险", "责任险", "财产险", "一切险", "综合险", "补充医疗", "医疗保障", "承保"],
    exclude=["社会保险缴纳", "保险柜", "虚拟仿真", "医疗保障信息平台"],
    regions=["上海"],
    kinds=["tender", "correction", "other"],
    lookback_days=30,
    sources=["zfcg_sh", "orgs"],
    request_interval=0,
    failure_threshold=3,
    archive=True,
)


class OneOffEngineeringTest(unittest.TestCase):
    def test_identifies_engineering_and_exhibition_insurance(self):
        one_offs = [
            "松江区中山街道SJC10014单元动迁安置房工程质量潜在缺陷保险服务采购公告",
            "同心路961、963号旧住房成套改造项目住宅工程质量潜在缺陷保险采购公告",
            "上海浦东国际机场运行保障基地项目建安工程一切险",
            "某新建市政道路建筑工程一切险采购公告",
            "某工地施工人员团体意外伤害保险竞争性磋商",
            "上海世博会博物馆借展藏品养护及运输保险（俄罗斯）单一来源公示",
        ]
        for t in one_offs:
            self.assertTrue(is_one_off_project(t), t)

    def test_keeps_normal_renewal_insurance(self):
        renewals = [
            "2027年静安区残疾人综合保险项目的公开招标公告",
            "中国银联2026年员工团体保险采购项目采购公告",
            "关于2027年上海文广物业管理有限公司车辆保险服务供应商比选的公告",
            "2027-2029年度上海申通地铁集团公众责任险项目",
            "上海青发市政管理有限公司采购车辆及船舶保险服务采购公告",
        ]
        for t in renewals:
            self.assertFalse(is_one_off_project(t), t)


class SameProjectMatchTest(unittest.TestCase):
    def test_matches_annual_and_multi_year_renewals(self):
        # 年度标（2026年保障年度在 2026-04 出结果，2027年保障年度在 2026-11 招标）
        self.assertTrue(is_same_project(
            "2027年静安区残疾人综合保险项目的公开招标公告",
            "上海市静安区残疾人联合会",
            date(2026, 11, 13),
            "2026年静安区残疾人综合保险项目的中标（成交）结果公告",
            "上海市静安区残疾人联合会",
            date(2026, 4, 21),
            CFG,
        ))
        # 三年期标（2024-2026 → 2027-2029）
        self.assertTrue(is_same_project(
            "2027-2029年度上海申通地铁集团公众责任险项目招标公告",
            None,
            date(2026, 11, 16),
            "2024-2026年度上海申通地铁集团公众责任险项目中标结果公告",
            None,
            date(2023, 11, 20),
            CFG,
        ))

    def test_matches_unit_plus_compatible_bucket_when_wording_shifts(self):
        # 曹路镇：2026年社区公共管理综合保险 → 2027年社会综合保险
        self.assertTrue(is_same_project(
            "2027年社会综合保险的竞争性磋商公告",
            "上海市浦东新区曹路镇人民政府",
            date(2026, 11, 13),
            "曹路镇2026年社区公共管理综合保险项目的中标（成交）结果公告",
            "上海市浦东新区曹路镇人民政府",
            date(2026, 2, 4),
            CFG,
        ))
        # 中国银联：2025年员工商业医疗保险 → 2026年员工团体保险
        self.assertTrue(is_same_project(
            "中国银联2026年员工团体保险采购项目采购公告",
            "中国银联",
            date(2026, 11, 13),
            "中国银联2025年员工商业医疗保险采购项目成交结果公告",
            "中国银联",
            date(2025, 10, 22),
            CFG,
        ))

    def test_rejects_same_round_retender_or_different_subsidiary_or_bucket(self):
        # 同轮第二次招标（只隔 25 天、同为 2026 年）不算往年标
        self.assertFalse(is_same_project(
            "2026年静安区老年人团体人身意外伤害专项保险项目（第二次）的公开招标公告",
            "上海市静安区养老服务发展中心",
            date(2026, 11, 2),
            "2026年静安区老年人团体人身意外伤害专项保险项目的公开招标公告",
            "上海市静安区养老服务发展中心",
            date(2026, 10, 8),
            CFG,
        ))
        # 不同子公司（文广物业 vs 文广实业）不互串
        self.assertFalse(is_same_project(
            "关于2027年上海文广物业管理有限公司车辆保险服务供应商比选的公告",
            "上海广播电视台（SMG）",
            date(2026, 11, 16),
            "关于上海文广实业有限公司车辆保险服务框架协议供应商遴选的招标公告",
            "上海广播电视台（SMG）",
            date(2024, 1, 12),
            CFG,
        ))
        # 同单位但不同险种（车险 vs 补充医疗）不互串
        self.assertFalse(is_same_project(
            "中国银联2026年员工团体保险采购项目采购公告",
            "中国银联",
            date(2026, 11, 13),
            "中国银联2025年总分公司财产保险采购项目成交结果公告",
            "中国银联",
            date(2025, 9, 18),
            CFG,
        ))
        # 不同街镇的同名「社会综合保险」不互串
        self.assertFalse(is_same_project(
            "2027年社会综合保险的竞争性磋商公告",
            "上海市浦东新区曹路镇人民政府",
            date(2026, 11, 13),
            "唐镇2026年社区公共管理综合保险项目的中标结果公告",
            "上海市浦东新区唐镇人民政府",
            date(2025, 10, 20),
            CFG,
        ))


class WinnerExtractionTest(unittest.TestCase):
    def test_extracts_from_html_table(self):
        html = (
            "<table><tr><td colspan='5'>中标（成交）信息</td></tr>"
            "<tr><th>序号</th><th>标项名称</th><th>中标供应商名称</th><th>中标供应商地址</th><th>中标金额</th></tr>"
            "<tr><td>1</td><td>2026年静安区残疾人综合保险项目</td>"
            "<td>中国人寿保险股份有限公司上海市分公司</td><td>银城路88号</td><td>22489000元</td></tr></table>"
        )
        self.assertEqual(extract_winners(html), ["中国人寿保险股份有限公司上海市分公司"])
        contract = "甲方：上海市某镇人民政府 乙方（供应商）：太平洋安信农业保险股份有限公司上海分公司 合同金额：200万元"
        self.assertEqual(extract_winners(contract), ["太平洋安信农业保险股份有限公司上海分公司"])

    def test_extracts_from_labeled_paragraphs_and_candidates(self):
        unionpay_text = (
            "三、成交结果 1、中选供应商：中国人民财产保险股份有限公司苏州市分公司 "
            "2、候选供应商：第一成交候选人：中国人民财产保险股份有限公司苏州市分公司 "
            "第二成交候选人：中国太平洋人寿保险股份有限公司上海分公司"
        )
        self.assertEqual(extract_winners(unionpay_text), ["中国人民财产保险股份有限公司苏州市分公司"])

        cand_only = "经评审，第一中标候选人：中国太平洋财产保险股份有限公司上海分公司；第二中标候选人：平安产险。"
        self.assertEqual(extract_winners(cand_only), ["中国太平洋财产保险股份有限公司上海分公司（第一候选）"])

    def test_extracts_from_flattened_text_without_picking_agency(self):
        flat = (
            "序号标项名称中标供应商名称中标供应商地址1曹路镇2026年社区公共管理综合保险项目"
            "太平洋安信农业保险股份有限公司上海分公司上海市共和新路3651号 "
            "采购代理机构信息：名称：上海某招标咨询有限公司"
        )
        self.assertEqual(extract_winners(flat), ["太平洋安信农业保险股份有限公司上海分公司"])


class EnrichAndRenderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "t.db"
        self.store = Store(self.db_path)

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def test_pick_best_prefers_result_with_winner_in_latest_round(self):
        cands = [
            Candidate("2025-10-10", "某局2026年车辆保险公开招标公告", "https://e/t", "tender"),
            Candidate("2025-11-05", "某局2026年车辆保险中标结果公告", "https://e/r", "result",
                      detail_fetcher=lambda: ["中国人民财产保险股份有限公司上海市分公司"]),
            Candidate("2024-11-01", "某局2025年车辆保险中标结果公告", "https://e/old", "result",
                      winners=["旧承保公司有限公司"]),
        ]
        best = pick_best(cands)
        self.assertEqual(best["status"], "found")
        self.assertEqual(best["date"], "2025-11-05")
        self.assertEqual(best["winners"], ["中国人民财产保险股份有限公司上海市分公司"])

    def test_enrich_pending_updates_store_mail_and_csv(self):
        # 本地存档先放一条去年的公告
        self.store.archive(Notice(
            source="zfcg_sh", source_id="old1",
            title="2026年某区机关车辆保险项目的公开招标公告",
            url="https://example.com/old1", publish_date="2025-11-10",
            purchaser="上海市某区机关事务管理局", kind="tender",
        ))
        # 本次新增两条待提醒：一条可续保车险、一条一次性工程险
        self.store.add(Notice(
            source="zfcg_sh", source_id="new1",
            title="2027年某区机关车辆保险项目的公开招标公告",
            url="https://example.com/new1", publish_date="2026-11-18",
            purchaser="上海市某区机关事务管理局", kind="tender",
        ), ["保险"])
        self.store.add(Notice(
            source="shggzy_zhcg", source_id="new2",
            title="某地块动迁安置房工程质量潜在缺陷保险服务采购公告",
            url="https://example.com/new2", publish_date="2026-11-18",
            kind="tender",
        ), ["保险"])

        rows = self.store.pending("2026-11-01")
        enriched = enrich_pending(rows, CFG, self.store, session=None)
        html = build_html(enriched, [], {"zfcg_sh": "上海政府采购网", "shggzy_zhcg": "综合采购"})
        self.assertIn("2025-11", html)
        self.assertIn("一次性工程险（跳过往年核查）", html)

        # 再写入一条带中标公司的记录，验证邮件高亮和 CSV 导出列
        self.store.update_extra("zfcg_sh", "new1", {"prior": {
            "status": "found", "date": "2025-11-28", "title": "结果公告",
            "url": "https://example.com/res1",
            "winners": ["中国人寿财产保险股份有限公司上海市分公司"],
        }})
        rows2 = self.store.pending("2026-11-01")
        html2 = build_html(rows2, [], {"zfcg_sh": "上海政府采购网", "shggzy_zhcg": "综合采购"})
        self.assertIn("曾经中标：<b>中国人寿财产保险股份有限公司上海市分公司</b>", html2)
        self.assertIn('href="https://example.com/res1"', html2)

        csv_path = Path(self.tmp.name) / "out.csv"
        export_csv(self.db_path, csv_path)
        csv_text = csv_path.read_text(encoding="utf-8-sig")
        self.assertIn("2025-11 发过｜原中标：中国人寿财产保险股份有限公司上海市分公司", csv_text)
        self.assertIn("一次性工程险（跳过）", csv_text)


if __name__ == "__main__":
    unittest.main()
