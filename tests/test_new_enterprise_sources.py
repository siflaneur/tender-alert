import json
import unittest
from datetime import date

from tender_alert.config import Config
from tender_alert.filters import match
from tender_alert.sources import baowu, cosco, ebscn, srcb

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


class FakeResponse:
    def __init__(self, text=""):
        self.text = text
        self.encoding = None

    def raise_for_status(self):
        pass


class FakeSession:
    def __init__(self, pages):
        self.pages = pages
        self.requests = []

    def get(self, url, params=None, headers=None):
        self.requests.append(url)
        return FakeResponse(self.pages[url])

    def post(self, url, data=None, headers=None):
        self.requests.append((url, data))
        return FakeResponse(self.pages[url])


def page(items):
    lis = "".join(
        f'<li><a href="/cms/cmscaigougg/{sid}.html" title="{title}">'
        f"<span>{title}</span></a><p><span>浏览次数：<span>1</span> 次</span>"
        f'<span class="Right Gray">{day.replace("-", "/")}</span></p></li>'
        for sid, title, day in items
    )
    return f'<div class="List2"><ul>{lis}</ul></div>'


class SrcbTest(unittest.TestCase):
    def test_maps_fields_and_stops_at_seen(self):
        sid1 = "a" * 32
        sid2 = "b" * 32
        session = FakeSession({
            srcb.LIST_URL: page([
                (sid1, "上海农商银行董事及高级管理人员责任险采购项目供应商征集公告", "2026-10-20"),
                (sid2, "旧公告", "2026-10-01"),
            ]),
        })
        state = {"seen": [sid2]}
        notices = srcb.fetch(session, CFG, SINCE, state)

        self.assertEqual(len(session.requests), 1)
        self.assertEqual([n.source_id for n in notices], [sid1, sid2])
        self.assertEqual(notices[0].title, "上海农商银行董事及高级管理人员责任险采购项目供应商征集公告")
        self.assertEqual(
            notices[0].url,
            f"https://pms2g.shrcb.com/cms/cmscaigougg/{sid1}.html",
        )
        self.assertEqual(notices[0].publish_date, "2026-10-20")
        self.assertEqual(state["seen"][:2], [sid1, sid2])

    def test_first_run_stops_below_window(self):
        fresh = "c" * 32
        old = "d" * 32
        session = FakeSession({
            srcb.LIST_URL: page([(fresh, "车辆保险采购公告", "2026-10-30")]),
            "https://pms2g.shrcb.com/cms/cmscaigougg/index_2.html":
                page([(old, "过期公告", "2026-09-01")]),
        })
        srcb.fetch(session, CFG, SINCE, {})
        self.assertEqual(len(session.requests), 2)

    def test_empty_first_page_raises(self):
        session = FakeSession({srcb.LIST_URL: "<html>改版了</html>"})
        with self.assertRaises(RuntimeError):
            srcb.fetch(session, CFG, SINCE, {})


def ebscn_search(items):
    cards = "".join(
        f'<a class="ch_bidding" href="/web/gdweb/index/procurementShow.do?articleId={sid}">'
        f'<span class="ch_bid_span">{title}</span>'
        f'<p><span>招标截止时间：</span><span>{deadline}</span></p></a>'
        for sid, title, deadline in items
    )
    return f"<html><title>集中采购管理平台</title><ul class='ch_con'>{cards}</ul></html>"


class EbsCnTest(unittest.TestCase):
    def test_search_only_opens_detail_for_keyword_hits(self):
        sid = "insurance1"
        detail_url = f"{ebscn.BASE}/web/gdweb/index/procurementShow.do?articleId={sid}"
        session = FakeSession({
            ebscn.SEARCH_URL: ebscn_search([
                (sid, "光大证券2026年度董监高责任险采购项目招标公告", "2026-11-20　17：00 :00"),
                ("risk1", "全面风险管理系统项目招标公告", "2026-11-21　17：00 :00"),
            ]),
            detail_url: '<div class="tender_detail"><p>光大证券股份有限公司</p>'
                        "<p>集中采购管理中心</p><p>2026年10月30日</p></div>",
        })
        state = {}
        notices = ebscn.fetch(session, CFG, SINCE, state)

        self.assertEqual([n.source_id for n in notices], [sid])
        self.assertEqual(notices[0].publish_date, "2026-10-30")
        self.assertEqual(notices[0].extra["deadline"], "2026-11-20 17:00")
        self.assertEqual(state["dates"][sid], "2026-10-30")
        post = session.requests[0]
        self.assertEqual(post[1]["article.searchName"], "险")
        self.assertEqual(post[1]["pi.pageSize"], "100")

    def test_cached_date_avoids_detail_request(self):
        sid = "insurance2"
        session = FakeSession({
            ebscn.SEARCH_URL: ebscn_search([
                (sid, "光大证券员工补充医疗保险采购公告", "2026-11-20 17:00:00"),
            ]),
        })
        state = {"dates": {sid: "2026-10-20"}}
        notices = ebscn.fetch(session, CFG, SINCE, state)
        self.assertEqual(notices[0].publish_date, "2026-10-20")
        self.assertEqual(len(session.requests), 1)

    def test_layout_change_raises(self):
        session = FakeSession({ebscn.SEARCH_URL: "<html>网站改版了</html>"})
        with self.assertRaises(RuntimeError):
            ebscn.fetch(session, CFG, SINCE, {})


def baowu_response(records):
    result = {"result": {"totalcount": len(records), "records": records}}
    return json.dumps({
        "code": 200,
        "content": json.dumps(result, ensure_ascii=False),
    }, ensure_ascii=False)


class BaowuSession:
    def __init__(self, records_by_term, details=None):
        self.records_by_term = records_by_term
        self.details = details or {}
        self.requests = []

    def post(self, url, data=None, headers=None):
        payload = json.loads(data)
        term = payload["condition"][1]["equal"]
        self.requests.append((url, data, headers, payload))
        return FakeResponse(baowu_response(self.records_by_term.get(term, [])))

    def get(self, url, params=None, headers=None):
        self.requests.append(url)
        return FakeResponse(self.details[url])


def baowu_item(sid, title, day="2026-10-29", custom=None, link=None):
    return {
        "id": sid,
        "title": title,
        "customtitle": custom or title,
        "webdate": f"{day} 10:00:00",
        "linkurl": link or f"/jyxx/018001/018001003/018001003002/{day.replace('-', '')}/{sid}.html",
        "content": f"[{title}] 招标公告",
    }


class BaowuTest(unittest.TestCase):
    def test_searches_recent_insurance_and_keeps_only_shanghai(self):
        shanghai = baowu_item(
            "shanghai",
            "上海申能新能源投资有限公司2026-2028年度团体保险项目",
            custom="上海申能新能源投资有限公司2026-2028年度团体保险项目[招标公告]",
        )
        baosteel = baowu_item(
            "baosteel",
            "宝山钢铁股份有限公司2027年度财产保险项目[招标公告]",
        )
        outside = baowu_item(
            "outside",
            "宝武物流资产有限公司南京分公司普通车辆保险[招标公告]",
        )
        risk = baowu_item("risk", "宁波宝新高风险作业管理信息化[招标公告]")
        session = BaowuSession({"险": [shanghai, baosteel, outside, risk]})

        notices = baowu.fetch(session, CFG, SINCE)

        self.assertEqual({n.source_id for n in notices}, {"shanghai", "baosteel"})
        notice = next(n for n in notices if n.source_id == "shanghai")
        self.assertEqual(notice.publish_date, "2026-10-29")
        self.assertEqual(notice.region, "上海")
        self.assertEqual(notice.notice_type, "招标公告")
        self.assertEqual(
            notice.url,
            "https://www.baohuabidding.cn/shbhgjzb/jyxx/018001/018001003/"
            "018001003002/20261029/shanghai.html",
        )
        first = session.requests[0]
        self.assertEqual(first[0], baowu.SEARCH_URL)
        self.assertIn('"equal": "险"', first[1])
        self.assertNotIn("\\u9669", first[1])
        self.assertEqual(first[2]["Content-Type"], "application/x-www-form-urlencoded; charset=UTF-8")
        self.assertEqual(first[3]["pn"], 0)
        self.assertEqual(first[3]["sdt"], "2026-09-30 00:00:00")
        self.assertEqual(len([r for r in session.requests if isinstance(r, tuple)]), 4)

    def test_unknown_project_opens_detail_and_uses_purchaser_or_location(self):
        item = baowu_item(
            "detail",
            "2026-2029年度员工福利保险项目[招标公告]",
        )
        session = BaowuSession(
            {"险": [item]},
            {
                item["linkurl"] and f"{baowu.BASE}/shbhgjzb{item['linkurl']}":
                    "<p>项目招标人为上海宝钢工程技术有限公司</p>"
                    "<p>项目地点：上海市宝山区</p>",
            },
        )

        notices = baowu.fetch(session, CFG, SINCE)

        self.assertEqual(len(notices), 1)
        self.assertEqual(notices[0].purchaser, "上海宝钢工程技术有限公司")
        self.assertEqual(notices[0].region, "上海")

    def test_bad_api_response_raises(self):
        session = BaowuSession({})
        session.post = lambda *args, **kwargs: FakeResponse("<html>接口改版了</html>")
        with self.assertRaises(RuntimeError):
            baowu.fetch(session, CFG, SINCE)


class CoscoSession:
    def __init__(self, pages):
        self.pages = pages
        self.requests = []

    def post(self, url, **kwargs):
        payload = kwargs.get("json") or kwargs.get("data")
        headers = kwargs.get("headers")
        self.requests.append((url, payload, headers))
        page = payload["basePageRequest"]["pageNo"]
        records, total = self.pages[page]
        body = {
            "code": 200,
            "success": True,
            "message": "success",
            "data": {"records": records, "total": total},
        }
        return FakeResponse(json.dumps(body, ensure_ascii=False))


def cosco_item(sid, title, region="上海市", province="310000", body=None):
    return {
        "id": sid,
        "annoName": title,
        "annoText": body or (
            "<p>项目名称：某采购项目</p>"
            "<p>采购人：中远海运集装箱运输有限公司</p>"
        ),
        "createTime": "2026-10-30 16:43:40",
        "annoStartDate": "2026-11-20 09:00:00",
        "region": region,
        "province": province,
        "regionId": province[:2] + "0000",
        "regionEn": "Shanghai" if region == "上海市" else "Other",
        "annoType": "0011004",
        "pAnnoType": "NonProcurementBidding",
    }


class CoscoTest(unittest.TestCase):
    def test_paginates_and_keeps_all_shanghai_notices(self):
        shanghai = cosco_item(
            "shanghai",
            "中远海运2027年度董监高责任险采购公告",
        )
        hainan = cosco_item(
            "hainan",
            "2027年度员工补充医疗保险采购公告",
            region="海南省",
            province="460000",
            body="<p>采购人：海南港航控股有限公司</p><p>上海供应商可参与。</p>",
        )
        irrelevant = cosco_item(
            "irrelevant",
            "通勤车辆包车招标公告",
            body="<p>采购人：中远海运集运</p><p>投标人保险齐全。</p>",
        )
        session = CoscoSession({
            1: ([shanghai, hainan], 3),
            2: ([irrelevant], 3),
        })

        notices = cosco.fetch(session, CFG, SINCE)

        # 上海的公告全部返回（全量存档要用），外地的不返回；保险筛选由主流程的 match 做
        self.assertEqual([n.source_id for n in notices], ["shanghai", "irrelevant"])
        self.assertEqual([n.source_id for n in notices if match(n, CFG, cosco.REGION_SCOPED)], ["shanghai"])
        self.assertEqual(notices[0].publish_date, "2026-10-30")
        self.assertEqual(notices[0].purchaser, "中远海运集装箱运输有限公司")
        self.assertEqual(notices[0].region, "上海")
        self.assertEqual(
            notices[0].url,
            "https://procurement.coscoshipping.com/portal/announce/announceInfo?id=shanghai",
        )
        self.assertEqual([r[1]["basePageRequest"]["pageNo"] for r in session.requests], [1, 2])
        self.assertEqual(session.requests[0][1]["startTime"], "2026-09-30 00:00:00")
        self.assertEqual(session.requests[0][1]["regionDictType"], "1")

    def test_body_insurance_words_do_not_create_match_text(self):
        item = cosco_item(
            "bidding",
            "数据中心改造项目招标公告",
            body="<p>采购人：上海某公司</p><p>要求车辆保险齐全。</p>",
        )
        session = CoscoSession({1: ([item], 1)})
        notices = cosco.fetch(session, CFG, SINCE)
        self.assertEqual([n.source_id for n in notices], ["bidding"])  # 会进全量存档
        self.assertIsNone(match(notices[0], CFG, cosco.REGION_SCOPED))  # 但正文里的「保险」不会让它被当成保险公告

    def test_bad_api_response_raises(self):
        session = CoscoSession({1: ([], 0)})
        session.post = lambda *args, **kwargs: FakeResponse("<html>接口改版了</html>")
        with self.assertRaises(RuntimeError):
            cosco.fetch(session, CFG, SINCE)


if __name__ == "__main__":
    unittest.main()
