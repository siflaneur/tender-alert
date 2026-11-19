"""上海企事业单位、金融基础设施及招标代理自有网站上的采购公告（第五批及后续扩展来源）。

这些单位的采购不走政府采购、或不同步到上海公共资源交易平台，只发在自有网站或代理机构公告栏：
- 广电与医院：SMG（上海广播电视台）、复旦肿瘤医院（院内需求调查早于政采一个多月）、复旦华山医院
- 科研院所：中科院上海高研院、有机所、微系统所
- 在沪金融基础设施与金融机构：中国银联、上海清算所、上海期货交易所、上海黄金交易所、城银清算、CIPS、中银国际证券、上海国际信托
- 市属重点集团：上海地产集团
- 头部招标代理机构（一家覆盖几十家医院/高校/国企/金融机构）：上海国际招标（SITC）、上海中世建设咨询、上海机电设备招标、上海财瑞建设管理、上海百通项目管理
- 全国行业平台筛上海：中烟电子采购平台（只留上海烟草集团）

绝大多数是静态列表页，复用高校的通用解析器（edu.parse_list）；上海国际信托、上海国际招标、上海中世建设咨询
走各自网页公开调用的免登录 JSON 接口。每个栏目只取第一页，每天抓一次。
单个单位失败只记警告，三分之一以上失败才整体报错（和高校来源一样）。
"""

import json
import logging
import re
from datetime import date
from urllib.parse import urlsplit

from ..config import Config
from ..models import Notice
from ._common import LegacyTLSAdapter, decode_html, outside_shanghai
from .edu import parse_list

NAME = "上海企事业单位·自有采购网站"
REGION_SCOPED = True  # 上海本地单位，或已在模块内筛掉外地分支

log = logging.getLogger(__name__)

# (key, 单位, [列表页地址, ...], 地区过滤方式)。key 用在 source_id 里，定了就不要改。
# 地区过滤方式：False=纯上海单位不过滤；True=全国平台只留标题带「上海」的；"local"=筛掉明确的外地分支/外省项目。
ORGS: list[tuple[str, str, list[str], bool | str]] = [
    # 广电：员工补充医疗、车辆保险框架、财产一切险+公众责任险都发在这里。这一页就是全部历史（约 3500 条、2.6MB）
    ("smg", "上海广播电视台（SMG）", ["https://caigou.smg.cn/review/ann/index.html"], False),
    # 医院：专家论证（院内需求调查）、采购公告、评标公示三个栏目
    ("shca", "复旦大学附属肿瘤医院", [f"https://www.shca.org.cn/Home/news/index/id/{n}/tid/380/fid/273.shtml" for n in (395, 281, 282)], False),
    ("huashan", "复旦大学附属华山医院", ["https://www.huashan.org.cn/ywgk/ztbgg.htm"], False),
    # 中科院在沪研究所：同一套 CMS，招标公告混在「通知公告」里
    ("cas_sari", "中国科学院上海高等研究院", ["https://sari.cas.cn/news/tzgg/"], False),
    ("cas_sioc", "中国科学院上海有机化学研究所", ["http://sioc.cas.cn/news/tzgg/"], False),
    ("cas_sim", "中国科学院上海微系统与信息技术研究所", ["http://www.sim.cas.cn/xwzx2016/tzgg2016/"], False),
    # 在沪金融基础设施、金融企业
    ("unionpay", "中国银联", [
        "https://supplier.unionpay.com/",
        "https://supplier.unionpay.com/jcepp-esp/notice/queryNewNoticePageList?searchType=01",
    ], "local"),
    ("shclearing", "银行间市场清算所（上海清算所）", ["https://www.shclearing.com.cn/xwgg/zcxx/"], False),
    ("shfe", "上海期货交易所", ["https://www.shfe.com.cn/publicnotice/collection/"], False),
    ("sge", "上海黄金交易所", ["https://www.sge.com.cn/gyjjs/cgzq"], False),
    ("cbcc", "城银清算服务有限责任公司", ["https://www.cbcc.cn/zxgg35/jzcg/"], False),
    ("cips", "跨境银行间支付清算有限责任公司（CIPS）", ["https://www.cips.com.cn/kjjqgs/zxzx/index.shtml"], False),
    ("boci", "中银国际证券", ["https://www.bocichina.com/main/investorrelations/newscenter/purchaseann/index.shtml"], False),
    # 市属重点集团
    ("shdcjt", "上海地产集团", ["https://www.shdcjt.com/disclosure/zbcg/index.html"], False),
    # 招标代理机构：替医院、高校、燃气、银行、在沪国企等单位招标，与综合采购等重复的靠跨站去重
    ("shbid", "上海机电设备招标有限公司（代理）", [
        "https://www.shbid.com/html/jidian/gonggaogongshi/zhaobiaogonggao/index.html",
        "https://www.shbid.com/html/jidian/gonggaogongshi/zhongbiaogonggao/index.html",
    ], "local"),
    ("cairui", "上海财瑞建设管理有限公司（代理）", [
        "https://www.cairuibidding.com.cn/CrZtb/portalweb/news",
        "https://www.cairuibidding.com.cn/CrZtb/portalweb/change",
    ], "local"),
    ("shbtpm", "上海百通项管科技有限公司（代理）", [
        "https://www.shbtpm.com/info/bid",
        "https://www.shbtpm.com/info/bid-deal",
    ], "local"),
    # 全国烟草系统的平台，上海烟草集团买财产险、车辆保险也发在这里；只留标题带「上海」的
    ("tobacco", "中烟电子采购平台", ["https://cgjy.tobacco.com.cn/zbNotice/index.jhtml"], True),
]

TRUST = ("shtrust", "上海国际信托")
TRUST_BASE = "https://www.shanghaitrust.com"
TRUST_PAGE = TRUST_BASE + "/responsibility/purchase"
TRUST_API = TRUST_BASE + "/middle-platform-gateway/wechatwork-content-biz/front/api/article/list"

SITC = ("sitc", "上海国际招标有限公司（代理）")
SITC_BASE = "https://www.shabidding.com"
SITC_PAGE = SITC_BASE + "/shgjzb/pc/index_pc_cn.html"
SITC_API = SITC_BASE + "/ekp/resource/news/sys_news_main/sysNewsMain.do"
SITC_TYPES = [
    (1, "13f89f0aecc9570a269328c4ea8b510d", "other"),   # 项目公告（招标、比选、磋商、变更等）
    (3, "16f434f75f02ce38c7ed58a413a82778", "result"),  # 结果公告
]

CWCC = ("cwcc", "上海中世建设咨询有限公司（代理）")
CWCC_BASE = "https://www.cwcc.net.cn"
CWCC_API = CWCC_BASE + "/Pages/News/News.aspx/GetNewPages"
CWCC_TYPES = [
    (1, "other"),   # 招标公告
    (2, "result"),  # 中标公告
    (3, "other"),   # 其他公告（比选、磋商、更正等）
]

_CWCC_DATE_RE = re.compile(r"(20\d{2})[-/](\d{1,2})[-/](\d{1,2})")


def _source_id(key: str, url: str) -> str:
    parts = urlsplit(url)
    return f"{key}:{parts.path}{'?' + parts.query if parts.query else ''}"


def fetch_org(session, key: str, org: str, urls: list[str], region_mode: bool | str,
              stop_before: str | None = None) -> list[Notice]:
    notices = []
    for list_url in urls:
        resp = session.get(list_url)
        resp.raise_for_status()
        items = parse_list(decode_html(resp), list_url, stop_before=stop_before)
        if not items:
            raise RuntimeError(f"{org}的公告列表一条也没解析出来，可能是网站改版了：{list_url}")
        for title, url, day in items:
            if region_mode is True and "上海" not in title:
                continue
            if region_mode == "local" and outside_shanghai(title):
                continue
            notices.append(Notice(source="orgs", source_id=_source_id(key, url), title=title, url=url,
                                  publish_date=day, purchaser=org, region="上海", kind="other"))
    return notices


def fetch_trust(session) -> list[Notice]:
    """上海国际信托：采购公告在「信息公开」栏目，网页通过这个公开接口取列表（免登录）。"""
    key, org = TRUST
    session.mount(TRUST_BASE, LegacyTLSAdapter())  # 服务器 TLS 配置较旧，默认设置握手失败
    resp = session.post(TRUST_API, json={"pageNo": 1, "pageSize": 30, "columnCode": "information_disclosure",
                                         "channelCode": "pc_service"}, headers={"Referer": TRUST_PAGE})
    resp.raise_for_status()
    try:
        body = resp.json()
        records = body["result"]["records"]
    except (ValueError, KeyError, TypeError) as exc:
        raise RuntimeError("上海国际信托列表接口返回格式变了") from exc
    notices = []
    for rec in records:
        title = re.sub(r"\s+", " ", rec.get("title") or "").strip()
        day = (rec.get("publishTime") or "")[:10]
        if not title or not rec.get("id") or not day:
            continue
        # 这个栏目还混着关联交易披露、催收机构公告，只留采购相关的
        if not re.search(r"采购|招标|磋商|谈判|询价|比选|单一来源|征集|遴选|中标|成交", title):
            continue
        notices.append(Notice(source="orgs", source_id=f"{key}:{rec['id']}", title=title,
                              url=f"{TRUST_BASE}/node?id={rec['id']}", publish_date=day,
                              purchaser=org, region="上海", kind="other"))
    return notices


def fetch_sitc(session) -> list[Notice]:
    """上海国际招标有限公司（SITC）：官网公示公告栏目的公开 JSON 接口（免登录）。"""
    key, org = SITC
    notices = []
    raw_count = 0
    for type_no, template_id, kind in SITC_TYPES:
        url = f"{SITC_API}?method=getNotice&type={type_no}&templateId={template_id}&showNotice=all&seacrhVal="
        resp = session.post(url, headers={"Referer": SITC_PAGE})
        resp.raise_for_status()
        try:
            records = resp.json()["data"]
        except (ValueError, KeyError, TypeError) as exc:
            raise RuntimeError("上海国际招标列表接口返回格式变了") from exc
        if not isinstance(records, list):
            raise RuntimeError("上海国际招标列表接口 data 不是列表")
        raw_count += len(records)
        for rec in records:
            title = re.sub(r"\s+", " ", rec.get("fdSubject") or "").strip()
            fd_id = str(rec.get("fdId") or "").strip()
            day = (rec.get("publishTime") or rec.get("time") or "")[:10]
            if not title or not fd_id or not day:
                continue
            if outside_shanghai(title):
                continue
            end_time = (rec.get("fdEndTime") or "").strip()
            extra = {"deadline": end_time[:16]} if end_time else {}
            notices.append(Notice(
                source="orgs", source_id=f"{key}:{fd_id}", title=title,
                url=f"{SITC_API}?method=view&fdId={fd_id}", publish_date=day,
                purchaser=org, region="上海", kind=kind, extra=extra,
            ))
    if raw_count == 0:
        raise RuntimeError("上海国际招标列表接口返回空列表，可能是网站改版了")
    return notices


def fetch_cwcc(session) -> list[Notice]:
    """上海中世建设咨询有限公司：官网「中世快讯」栏目的公开分页接口（免登录）。"""
    key, org = CWCC
    notices = []
    raw_count = 0
    for type_no, kind in CWCC_TYPES:
        resp = session.post(
            CWCC_API,
            json={"pageindex": "1", "typeindex": str(type_no)},
            headers={"Referer": f"{CWCC_BASE}/Pages/News/News.aspx?PID={type_no}"},
        )
        resp.raise_for_status()
        try:
            inner = json.loads(resp.json()["d"])
            records = inner["infos"]
        except (ValueError, KeyError, TypeError) as exc:
            raise RuntimeError("上海中世建设咨询列表接口返回格式变了") from exc
        if str(inner.get("status")) != "1" or not isinstance(records, list):
            raise RuntimeError("上海中世建设咨询列表接口状态异常")
        raw_count += len(records)
        for rec in records:
            title = re.sub(r"\s+", " ", rec.get("Name") or "").strip()
            rec_id = str(rec.get("Id") or "").strip()
            m = _CWCC_DATE_RE.search(rec.get("Date") or "")
            if not title or not rec_id or not m:
                continue
            day = date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
            if outside_shanghai(title):
                continue
            notices.append(Notice(
                source="orgs", source_id=f"{key}:{type_no}-{rec_id}", title=title,
                url=f"{CWCC_BASE}/Pages/News/NewsDetail.aspx?newstype={type_no}&newsid={rec_id}",
                publish_date=day, purchaser=org, region="上海", kind=kind,
            ))
    if raw_count == 0:
        raise RuntimeError("上海中世建设咨询列表接口返回空列表，可能是网站改版了")
    return notices


API_ORGS = [
    (TRUST[0], TRUST[1], fetch_trust),
    (SITC[0], SITC[1], fetch_sitc),
    (CWCC[0], CWCC[1], fetch_cwcc),
]


def sub_name(sub_key: str) -> str | None:
    for key, org, _, _ in ORGS:
        if key == sub_key:
            return org
    for key, org, _ in API_ORGS:
        if key == sub_key:
            return org
    return None


def fetch(session, cfg: Config, since: date) -> list[Notice]:
    found: dict[str, Notice] = {}
    failed: list[str] = []
    sub_health: dict[str, tuple[str, str | None]] = {}
    jobs = [(key, org, fetch_org, (key, org, urls, sh, since.isoformat())) for key, org, urls, sh in ORGS]
    jobs.extend((key, org, func, ()) for key, org, func in API_ORGS)
    for sub_key, org, func, args in jobs:
        try:
            batch = func(session, *args)
        except Exception as e:  # 一个单位的网站出问题不影响别的，单独记连续失败次数
            err = f"{type(e).__name__}: {str(e)[:200]}"
            log.warning("%s 抓取失败：%s", org, err[:140])
            failed.append(org)
            sub_health[f"orgs:{sub_key}"] = (org, err)
            continue
        sub_health[f"orgs:{sub_key}"] = (org, None)
        for n in batch:
            if n.publish_date[:10] >= since.isoformat():
                found.setdefault(n.source_id, n)
    if len(failed) * 3 >= len(jobs):
        raise RuntimeError(f"{len(jobs)} 个单位里有 {len(failed)} 个抓取失败：{'、'.join(failed[:8])}")
    session.sub_health = sub_health
    return list(found.values())
