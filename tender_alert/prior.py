"""往年同项目续标核查与曾经中标公司提取。

只对本次准备发信提醒的新命中公告做核查：
1. 先识别一次性工程类保险（建安工程一切险、工程质量潜在缺陷保险 IDI、施工人员意外险等），
   直接标记为 `one_off` 并跳过往年查询，避免把别的地块/工地项目误认成续标。
2. 对可续保项目回查近 3 年（兼容 1 年期及「2027-2029」这类 2–3 年一签的保单）：
   依次查本地数据库（archive + notices）、上海政府采购网公开接口、国企采购公开接口，
   以及对应机构自有公开列表（中国银联、SMG、上海国际招标、东航、机场、国泰海通）。
3. 若定位到上一轮中标/成交结果或合同公告，提取曾经的中标保险公司（或第一候选人）写入
   `notices.extra["prior"]`，供邮件展示和 CSV 导出。任何外部查询异常只记警告，不阻断发信。
"""

import html as html_lib
import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import date, timedelta

from bs4 import BeautifulSoup

from .config import Config
from .models import classify_kind
from .sources import airport, ccgp, ceair, gtht, orgs, shggzy, sse, zfcg_sh
from .sources._common import LegacyTLSAdapter, decode_html
from .sources.edu import parse_list

log = logging.getLogger(__name__)

LOOKBACK_YEARS_DAYS = 365 * 3 + 180  # 近 3 年半，覆盖「2024-2026 → 2027-2029」这类三年期保单
MIN_CYCLE_GAP_DAYS = 150  # 标题没写保障年度时，至少相隔约 5 个月才算上一轮（排除同轮重招/更正）

# 一次性工程类/单次活动运输类保险：不按年度续保，跳过往年同标查询
_ONE_OFF_RE = re.compile(
    r"潜在缺陷保?险|工程质量保?险|(?:建筑|安装|建安|建设)?工程一切险|建安一切险|"
    r"建筑工程险|安装工程险|建工险|建工意外险|施工人员.*意外|建筑施工.*意外|"
    r"借展.*运输保?险|展览.*往返运输及保险"
)

# 险种分桶：同一采购单位买车险和买补充医疗不是同一个标，分桶必须相容
_BUCKETS: list[tuple[str, re.Pattern]] = [
    ("auto", re.compile(r"车险|车辆.*险|机动车.*险|公务用车.*险")),
    ("medical_life", re.compile(
        r"补充医疗|医疗保障|医疗补助|团体医疗|商业医疗|团体意外|人身意外|"
        r"员工团体保险|员工商业保险|员工人身保险|职工商业保险|综合福利保险|老年人.*意外|失独|住院.*保险"
    )),
    ("liability_pub", re.compile(r"公众责任")),
    ("liability_emp", re.compile(r"雇主责任|雇主险")),
    ("liability_do", re.compile(r"董监高|董事.*责任|董责险")),
    ("liability_med", re.compile(r"医疗.*责任|医责险")),
    ("liability_safe", re.compile(r"安责险|安全生产责任")),
    ("liability_other", re.compile(r"维修人责任|物流责任|校方责任")),
    ("property", re.compile(r"财产险|财产保险|财产一切险|企财险|抵押物保险|机损险|机器损坏|运营线路保险")),
    ("vessel", re.compile(r"船舶保险|船舶险")),
    ("community", re.compile(r"社区.*保险|社会综合保险|公共管理.*保险|阳光综合险|残疾人综合保险|为老服务.*保险")),
]
# 街镇社区保险常在「社区公共管理综合保险」与「社区管理公众责任保险」之间换叫法，视为相容险种
_COMPATIBLE_BUCKETS = {("community", "liability_pub"), ("liability_pub", "community")}

_YEAR_SPAN_RE = re.compile(r"(?<!\d)(20\d{2})\s*[-—–~至/]\s*(20\d{2})\s*年?度?")
_SHORT_SPAN_RE = re.compile(r"(?<!\d)(2\d)\s*[-—–~至/]\s*(2\d)\s*年")
_SINGLE_YEAR_RE = re.compile(r"(?<!\d)(20\d{2})\s*年?度")
_YEAR_NIAN_RE = re.compile(r"(?<!\d)(20\d{2})\s*年")

_PREFIX_CLEAN_RE = re.compile(
    r"^(?:【[^】]{1,20}】|\[[^\]]{1,20}\]|供应商征集公告[（(]|关于)+"
)
_NOISE_RE = re.compile(
    r"(?:20\d{2}\s*[-—–~至/]\s*20\d{2}\s*年?度?|2\d\s*[-—–~至/]\s*2\d\s*年|20\d{2}\s*年?度?|"
    r"[（(【\[][^）)】\]]*(?:二次|第二次|第三次|重新|重招|更正|变更|延期|终止|流标|废标|失败|包件|标包|第\d+号标)[^）)】\]]*[）)】\]]|"
    r"标包[一二三四1-9][、，,]?)"
)
_TAIL_CLEAN_RE = re.compile(
    r"(?:的?(?:公开|邀请)?(?:招标|采购|竞争性磋商|竞争性谈判|询价|比选|询比|竞价|单一来源|征集|遴选|"
    r"中标|成交|中选|入围|候选人|结果|更正|变更|澄清|延期|终止|废标|流标|失败|合同|验收|方式|信息)*"
    r"(?:项目|服务|产品及服务|供应商|公告|公示|文件)+|[）)]|[-—_]+)+$"
)
_GENERIC_CORE_RE = re.compile(
    r"^(?:上海市?)?(?:车辆|机动车|公务用车|员工|职工|团体|商业|补充医疗|医疗保障|医疗补助|团体医疗|"
    r"医疗责任|医责|公众责任|雇主责任|雇主|董监高责任|董监高|董事及高级管理人员责任|董责|"
    r"安全生产责任|安责|校方责任|物流责任|维修人责任|人身意外|团体意外|意外伤害|意外|"
    r"财产一切|财产综合|财产|企财|机器损坏|机损|船舶|综合福利|社会综合|社区公共管理|社区管理|"
    r"社区|街道社区|综合|责任|保险|车险|意外险|责任险|财产险|一切险|综合险|医责险|安责险|董责险|"
    r"雇主险|团意险|企财险|机损险|船舶险|服务|采购|管理|年度|项目|及|与|和|的)+$"
)

# 从标题中识别下属子公司/具体机构名，避免同集团不同子公司互相串标，也用于无采购人字段时锚定单位
_SUB_ORG_RE = re.compile(
    r"([一-龥]{2,18}?(?:物业管理|物业|实业|维护保障|维保|数据|技术|信息|物流|市政管理|磁浮交通|"
    r"东方希杰|联合发展|地铁集团|申通地铁|集团|银行(?:上海)?分行|银行|证券|信托|"
    r"社区卫生服务中心|卫生服务中心|医院|学院|大学|学校|中学|小学|幼儿园|研究院|研究所|"
    r"博物馆|图书馆|支队|总队|管理局|航道局|委员会|残联|残疾人联合会|福利院)"
    r"(?:股份)?(?:有限)?(?:责任)?(?:公司)?)"
)
_DISTRICT_RE = re.compile(
    r"(浦东新区|黄浦区|徐汇区|长宁区|静安区|普陀区|虹口区|杨浦区|闵行区|宝山区|嘉定区|金山区|松江区|青浦区|奉贤区|崇明区)"
)
_INDEPENDENT_UNIT_RE = re.compile(
    r"(?:医院|卫生服务中心|大学|学院|学校|中学|小学|幼儿园|公司|集团|银行|分行|证券|信托|研究所|研究院|镇|街道)$"
)
_TITLE_ORG_NOISE_RE = re.compile(r"保险|车险|意外险|责任险|财产险|综合险|一切险|医疗责任|补充医疗|招标|采购|承保")

# 结果正文中提取中标/成交公司
_HEADER_COL_RE = re.compile(r"^(?:中标|成交|中选|入选|入围|拟定唯一)?(?:供应商|单位|人|机构|公司)(?:名称)?$")
_HEADER_SKIP_RE = re.compile(r"地址|得分|金额|报价|代码|联系人|电话|评审|序号|备注|品目|标的|包名")
_FINAL_LABEL_RE = re.compile(
    r"(?:中标供应商|成交供应商|中选供应商|入选供应商|拟定唯一供应商|"
    r"中标单位|成交单位|中选单位|中标人|成交人|中选人|承保单位|承保机构|承保公司|"
    r"乙方(?:[（(]供应商[）)])?)(?:名称)?\s*[：:]\s*"
    r"([^\n。；;|<]{4,60})"
)
_CAND_LABEL_RE = re.compile(
    r"第一(?:中标|成交|中选)?候选(?:人|供应商|单位)(?:名称)?\s*[：:]\s*([^\n。；;|<]{4,60})"
)
_COMPANY_RE = re.compile(
    r"([一-龥()（）]{2,26}?(?:股份有限公司|有限责任公司|有限公司)"
    r"(?:[一-龥()（）]{0,16}?(?:分公司|支公司|中心支公司|营业部))?)"
)
_INSURER_RE = re.compile(
    r"([一-龥()（）]{2,20}?(?:财产保险|人寿保险|养老保险|健康保险|农业保险|航运保险|再保险|"
    r"保险经纪|保险公估|保险代理|保险[（(]集团[)）]|保险股份)"
    r"[一-龥()（）]{0,16}?(?:股份有限公司|有限责任公司|有限公司)"
    r"(?:[一-龥()（）]{0,16}?(?:分公司|支公司|中心支公司|营业部))?)"
)
_FOOTER_CUT_RE = re.compile(r"五、评审专家|评审专家名单|采购代理机构信息|凡对本次公告内容提出询问")


@dataclass
class Candidate:
    publish_date: str  # YYYY-MM-DD
    title: str
    url: str
    kind: str  # tender / result / other
    purchaser: str | None = None
    winners: list[str] | None = None
    detail_fetcher: object = None  # 选中后才按需请求一次详情页取中标人，节省请求


def is_one_off_project(title: str) -> bool:
    """是否为一次性工程类或单次展览运输类保险。"""
    return bool(_ONE_OFF_RE.search(title or ""))


def coverage_years(title: str) -> tuple[int, int] | None:
    """提取标题里的保障年度，如「2027-2029年度」→ (2027, 2029)，「2027年」→ (2027, 2027)。"""
    m = _YEAR_SPAN_RE.search(title)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = _SHORT_SPAN_RE.search(title)
    if m:
        return 2000 + int(m.group(1)), 2000 + int(m.group(2))
    m = _SINGLE_YEAR_RE.search(title) or _YEAR_NIAN_RE.search(title)
    if m:
        y = int(m.group(1))
        return y, y
    return None


def insurance_buckets(title: str) -> set[str]:
    return {name for name, rx in _BUCKETS if rx.search(title)}


def buckets_compatible(a: set[str], b: set[str]) -> bool:
    if not a or not b:
        return True
    if a & b:
        return True
    return any((x, y) in _COMPATIBLE_BUCKETS for x in a for y in b)


def core_topic(title: str) -> str:
    """去掉年份、批次括号和公告后缀，留下核心项目短语。"""
    s = _PREFIX_CLEAN_RE.sub("", (title or "").strip())
    s = _NOISE_RE.sub("", s)
    s = _TAIL_CLEAN_RE.sub("", s)
    s = re.sub(r"[\s“”\"'、，,：:（）()\[\]【】]+", "", s)
    return s


def unit_anchor(title: str, purchaser: str | None) -> str | None:
    """提取用于跨年度锚定采购人的简称（如「惠南镇」「静安区残疾人联合会」「中国银联」）。"""
    raw = (purchaser or "").strip()
    if raw and "（代理）" not in raw and raw != "—":
        u = re.sub(r"[（(]本级[）)]", "", raw)
        # 街镇政府保留「XX镇/XX街道」最稳
        town = re.search(r"([一-龥]{2,6}(?:镇|街道))(?:人民政府|办事处)?$", u)
        if town:
            return town.group(1)
        u = re.sub(r"^上海市", "", u)
        u = re.sub(r"(?:股份有限公司|有限责任公司|有限公司|人民政府)$", "", u)
        u = u.strip()
        if len(u) >= 3:
            return u
    cleaned = _NOISE_RE.sub(" ", _PREFIX_CLEAN_RE.sub("", title or "")).strip()
    cleaned = re.sub(r"^(?:关于)+", "", cleaned).strip()
    m = _SUB_ORG_RE.search(cleaned)
    if m and not _TITLE_ORG_NOISE_RE.search(m.group(1)):
        u = re.sub(r"^(?:上海|上海市)", "", m.group(1))
        u = re.sub(r"(?:股份有限公司|有限责任公司|有限公司)$", "", u)
        return u if len(u) >= 3 else m.group(1)
    # 镇/区前缀写在标题开头的情况（如「唐镇社区公共管理综合保险」「虹口区消防救援支队…」）
    m2 = re.match(r"^(?:上海市?)?([一-龥]{2,10}?(?:镇|街道|支队|总队|医院|学院|大学|博物馆|航道管理局|干部学院|集团|银行|分行))", cleaned)
    if m2 and not _TITLE_ORG_NOISE_RE.search(m2.group(1)):
        return m2.group(1)
    return None


def _sub_org_stem(title: str) -> str | None:
    cleaned = _NOISE_RE.sub(" ", _PREFIX_CLEAN_RE.sub("", title or "")).strip()
    m = _SUB_ORG_RE.search(cleaned)
    if not m or _TITLE_ORG_NOISE_RE.search(m.group(1)):
        return None
    s = re.sub(r"^(?:上海|上海市)", "", m.group(1))
    s = re.sub(r"(?:管理)?(?:股份有限公司|有限责任公司|有限公司|公司)$", "", s)
    return s if len(s) >= 2 else None


def is_earlier_cycle(cur_title: str, cur_date: date, cand_title: str, cand_date: date) -> bool:
    days = (cur_date - cand_date).days
    if days <= 0 or days > LOOKBACK_YEARS_DAYS:
        return False
    cy, py = coverage_years(cur_title), coverage_years(cand_title)
    if cy and py:
        if py[0] >= cy[0] or (cy[0] - py[0]) > 4:
            return False
        return days >= 60  # 跨保障年度也至少间隔两个月，避免同批多标段误判
    return days >= MIN_CYCLE_GAP_DAYS


def _char_bigrams(s: str) -> set[str]:
    return {s[i:i + 2] for i in range(len(s) - 1)} if len(s) >= 2 else ({s} if s else set())


def is_same_project(
    cur_title: str,
    cur_purchaser: str | None,
    cur_date: date,
    cand_title: str,
    cand_purchaser: str | None,
    cand_date: date,
    cfg: Config,
) -> bool:
    """判断候选历史公告是否为当前公告的往年同一项目。"""
    if not cand_title or is_one_off_project(cand_title):
        return False
    if any(w in cand_title for w in cfg.exclude):
        return False
    if not any(k in cand_title for k in cfg.keywords):
        return False
    if not is_earlier_cycle(cur_title, cur_date, cand_title, cand_date):
        return False

    b_cur, b_cand = insurance_buckets(cur_title), insurance_buckets(cand_title)
    if not buckets_compatible(b_cur, b_cand):
        return False

    # 如果两边标题都明确写了下属子公司/具体单位（如「文广物业」vs「文广实业」），必须一致
    sub_cur, sub_cand = _sub_org_stem(cur_title), _sub_org_stem(cand_title)
    if sub_cur and sub_cand and sub_cur not in sub_cand and sub_cand not in sub_cur:
        return False

    c_cur, c_cand = core_topic(cur_title), core_topic(cand_title)
    u_cur = unit_anchor(cur_title, cur_purchaser)
    # 当前公告既没有真实采购人字段、标题里也未写明具体采购单位时（如综合采购「2027年度医疗责任保险服务项目」），
    # 无法确认对方身份，绝不拿通用险种名去跨库乱认往年标
    if not u_cur:
        return False

    u_cand = unit_anchor(cand_title, cand_purchaser)
    cur_scope = f"{cur_title} {cur_purchaser or ''}"
    cand_scope = f"{cand_title} {cand_purchaser or ''}"
    unit_hit = (u_cur in cand_scope) or bool(u_cand and u_cand in cur_scope)

    specific_cur = len(c_cur) >= 6 and not _GENERIC_CORE_RE.match(c_cur)
    specific_cand = len(c_cand) >= 6 and not _GENERIC_CORE_RE.match(c_cand)

    if not unit_hit:
        # 仅在「同行政区政府直属机构更名」且标题自带相同区名+长专项名时放行
        # （例如：宝山区计生指导中心 → 宝山区人口和家庭发展指导中心，「宝山区失独家庭特扶对象住院综合保险」）；
        # 医院、学校、公司、街镇等独立主体采购人不同时一律拒绝
        if (
            u_cand
            and specific_cur
            and specific_cand
            and min(len(c_cur), len(c_cand)) >= 10
            and (c_cur in c_cand or c_cand in c_cur)
            and not _INDEPENDENT_UNIT_RE.search(u_cur)
            and not _INDEPENDENT_UNIT_RE.search(u_cand)
        ):
            d_cur = _DISTRICT_RE.search(cur_title or "")
            d_cand = _DISTRICT_RE.search(cand_title or "")
            if d_cur and d_cand and d_cur.group(1) == d_cand.group(1):
                return True
        return False

    # 同一采购单位确认后，再比项目名或险种分桶
    if specific_cur and specific_cand and (c_cur in c_cand or c_cand in c_cur):
        return True
    if b_cur and b_cand:
        return True
    bg_a, bg_b = _char_bigrams(c_cur), _char_bigrams(c_cand)
    if bg_a and bg_b and len(bg_a & bg_b) / len(bg_a | bg_b) >= 0.45:
        return True

    return False


def _clean_company(raw: str, purchaser: str | None = None) -> list[str]:
    text = re.sub(r"\s+", " ", raw or "").strip(" ：:、，,。；;（）()")
    if not text:
        return []
    found: list[str] = []
    for m in _COMPANY_RE.finditer(text):
        name = m.group(1).strip("（）()")
        # 去掉表格连写时粘在前面的项目名后缀（如「项目中国人寿…」→「中国人寿…」）
        name = re.sub(r"^.*?(?:项目|标段|包件|标项|服务|保险)(?:[（(][^）)]{1,8}[）)])?(?=(?:中国|太平洋|平安|太平|新华|泰康|阳光|大地|中华联合|大家|华安|永安|天安|紫金|国泰|众安|安盛|中银|申能|国任|鼎和|英大|锦泰|东海|三井|美亚|苏黎世|安联|江泰|中怡|达信|韦莱|[一-龥]{2,6}(?:财产|人寿|养老|健康|农业|航运|保险)))", "", name)
        if len(name) < 6 or len(name) > 42:
            continue
        if purchaser and (name == purchaser or name in purchaser):
            continue
        if any(bad in name for bad in ("招标", "代理", "咨询", "评审", "交易中心")):
            if "保险经纪" not in name and "保险公估" not in name and "保险代理" not in name:
                continue
        if name not in found:
            found.append(name)
    return found


def extract_winners(html_or_text: str, supplier_field: str | None = None, purchaser: str | None = None) -> list[str]:
    """从结果/合同/单一来源正文或结构化字段里提取中标公司（若是候选人公示则带『（第一候选）』）。"""
    winners: list[str] = []
    if supplier_field:
        for part in re.split(r"[,，、;；/]", str(supplier_field)):
            for c in _clean_company(part, purchaser):
                if c not in winners:
                    winners.append(c)
        if winners:
            return winners[:4]

    if not html_or_text:
        return []
    html_or_text = html_lib.unescape(html_or_text)

    soup = BeautifulSoup(html_or_text, "lxml") if "<" in html_or_text else None
    if soup is not None:
        for table in soup.find_all("table"):
            rows = table.find_all("tr")
            if len(rows) < 2:
                continue
            for r_idx, hrow in enumerate(rows[:-1]):
                headers = [re.sub(r"\s+", "", cell.get_text()) for cell in hrow.find_all(["th", "td"])]
                col_idxs = [
                    idx for idx, h in enumerate(headers)
                    if _HEADER_COL_RE.search(h) and not _HEADER_SKIP_RE.search(h)
                ]
                if not col_idxs:
                    continue
                for tr in rows[r_idx + 1:]:
                    cells = tr.find_all(["td", "th"])
                    for idx in col_idxs:
                        if idx < len(cells):
                            for c in _clean_company(cells[idx].get_text(" ", strip=True), purchaser):
                                if c not in winners:
                                    winners.append(c)
                break
        if winners:
            return winners[:4]

    text = soup.get_text(" ", strip=True) if soup is not None else html_or_text
    text = re.sub(r"\s+", " ", text)
    cut = _FOOTER_CUT_RE.search(text)
    body = text[:cut.start()] if cut else text

    for m in _FINAL_LABEL_RE.finditer(body):
        for c in _clean_company(m.group(1), purchaser):
            if c not in winners:
                winners.append(c)
    if winners:
        return winners[:4]

    for m in _CAND_LABEL_RE.finditer(body):
        for c in _clean_company(m.group(1), purchaser):
            tag = f"{c}（第一候选）"
            if tag not in winners:
                winners.append(tag)
    if winners:
        return winners[:4]

    # 表格被压成纯文本行（如政采云部分公告没有标准 th）时，在正文里找持牌保险/保险经纪机构全称
    for m in _INSURER_RE.finditer(body):
        for c in _clean_company(m.group(1), purchaser):
            if c not in winners:
                winners.append(c)
    return winners[:4]


def _parse_date(s: str | None) -> date | None:
    if not s or len(s) < 10:
        return None
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        return None


def _search_keywords(title: str, purchaser: str | None) -> list[str]:
    """生成 1–2 个精炼且在原文中连续出现的检索词：先搜具体核心词段，再退到采购单位简称。"""
    kws: list[str] = []
    stripped = _TAIL_CLEAN_RE.sub("", _NOISE_RE.sub(" ", _PREFIX_CLEAN_RE.sub("", title or "")))
    for seg in re.findall(r"[一-龥]{4,20}", stripped):
        if not _GENERIC_CORE_RE.match(seg) and seg not in kws:
            kws.append(seg)
            break
    u = unit_anchor(title, purchaser)
    if u and u not in kws:
        kws.append(u)
    return kws[:2]


def _collect_local(store, cur_title: str, cur_purchaser: str | None, cur_date: date, since_3y: date,
                   cfg: Config, cache: dict | None = None) -> list[Candidate]:
    if cache is not None:
        if "_local_rows" not in cache:
            cache["_local_rows"] = store.history_insurance_rows("1900-01-01", "9999-12-31")
        rows = cache["_local_rows"]
    else:
        rows = store.history_insurance_rows(since_3y.isoformat(), cur_date.isoformat())
    out: list[Candidate] = []
    for row in rows:
        d = _parse_date(row["publish_date"])
        if not d or d < since_3y or d > cur_date:
            continue
        if not is_same_project(cur_title, cur_purchaser, cur_date, row["title"], row["purchaser"], d, cfg):
            continue
        kind = row["kind"] if row["kind"] != "other" else classify_kind(row["notice_type"], row["title"])
        out.append(Candidate(
            publish_date=d.isoformat(),
            title=row["title"],
            url=row["url"],
            kind=kind,
            purchaser=row["purchaser"],
        ))
    return out


def _collect_zfcg(session, cur_title: str, cur_purchaser: str | None, cur_date: date, since_3y: date, cfg: Config) -> list[Candidate]:
    kws = _search_keywords(cur_title, cur_purchaser)
    if not kws:
        return []
    out: list[Candidate] = []
    seen: set[str] = set()
    # 先查结果与合同（能直接拿到中标公司），没命中再查往年采购公告
    result_codes = [("ZcyAnnouncement4", "result"), ("77-298537", "result"), ("ZcyAnnouncement5", "result")]
    tender_codes = [("ZcyAnnouncement2", "tender"), ("ZcyAnnouncement1", "tender"), ("77-987173", "tender")]

    for kw in kws:
        for code, kind in result_codes:
            for rec in zfcg_sh._search(session, code, kw, True, since_3y):
                aid = rec.get("articleId") or ""
                if aid in seen:
                    continue
                n = zfcg_sh._notice(rec, code)
                d = _parse_date(n.publish_date)
                if not d or not is_same_project(cur_title, cur_purchaser, cur_date, n.title, n.purchaser, d, cfg):
                    continue
                seen.add(aid)
                winners = extract_winners(rec.get("content") or "", rec.get("supplierName"), n.purchaser or cur_purchaser)
                out.append(Candidate(d.isoformat(), n.title, n.url, kind, n.purchaser, winners))
        if any(c.winners for c in out):
            return out

    # 有些公告标题只写「2027年社会综合保险」，单位名只在采购人字段里：用「采购人 + 保险」做一次全文检索兜底
    u = unit_anchor(cur_title, cur_purchaser)
    if u:
        buyer_q = f"{u} 保险"
        for rec in zfcg_sh._search(session, "ZcyAnnouncement4", buyer_q, False, since_3y):
            aid = rec.get("articleId") or ""
            if aid in seen:
                continue
            n = zfcg_sh._notice(rec, "ZcyAnnouncement4")
            d = _parse_date(n.publish_date)
            if not d or not is_same_project(cur_title, cur_purchaser, cur_date, n.title, n.purchaser, d, cfg):
                continue
            seen.add(aid)
            winners = extract_winners(rec.get("content") or "", rec.get("supplierName"), n.purchaser or cur_purchaser)
            out.append(Candidate(d.isoformat(), n.title, n.url, "result", n.purchaser, winners))
        if any(c.winners for c in out):
            return out

    for kw in kws[:1]:
        for code, kind in tender_codes:
            for rec in zfcg_sh._search(session, code, kw, True, since_3y):
                aid = rec.get("articleId") or ""
                if aid in seen:
                    continue
                n = zfcg_sh._notice(rec, code)
                d = _parse_date(n.publish_date)
                if not d or not is_same_project(cur_title, cur_purchaser, cur_date, n.title, n.purchaser, d, cfg):
                    continue
                seen.add(aid)
                # 单一来源公示正文里常有「拟定唯一供应商」
                winners = extract_winners(rec.get("content") or "", rec.get("supplierName"), n.purchaser or cur_purchaser) if code == "ZcyAnnouncement1" else []
                out.append(Candidate(d.isoformat(), n.title, n.url, kind, n.purchaser, winners))
        if out:
            break
    return out


def _collect_shggzy(session, cur_title: str, cur_purchaser: str | None, cur_date: date, since_3y: date, cfg: Config) -> list[Candidate]:
    kws = _search_keywords(cur_title, cur_purchaser)
    if not kws:
        return []
    out: list[Candidate] = []
    for kw in kws:
        res = shggzy._query(session, [kw], since_3y, 0)
        for rec in res.get("records") or []:
            n = shggzy._notice(rec)
            d = _parse_date(n.publish_date)
            if not d or not is_same_project(cur_title, cur_purchaser, cur_date, n.title, n.purchaser, d, cfg):
                continue
            kind = n.kind if n.kind != "other" else classify_kind(n.notice_type, n.title)
            winners = extract_winners(rec.get("content") or "", None, n.purchaser or cur_purchaser) if kind == "result" else []
            out.append(Candidate(d.isoformat(), n.title, n.url, kind, n.purchaser, winners))
        if out:
            break
    return out


def _fetch_html_winners(session, url: str, purchaser: str | None) -> list[str]:
    resp = session.get(url)
    resp.raise_for_status()
    return extract_winners(decode_html(resp), None, purchaser)


def _collect_unionpay(session, cur_title: str, cur_purchaser: str | None, cur_date: date, since_3y: date, cfg: Config) -> list[Candidate]:
    url = "https://supplier.unionpay.com/jcepp-esp/notice/queryNewNoticePageList"
    resp = session.get(url, params={
        "searchType": "01",
        "title": "保险",
        "sendTimeMin": f"{since_3y.isoformat()} 00:00:00",
        "sendTimeMax": f"{cur_date.isoformat()} 23:59:59",
    })
    resp.raise_for_status()
    out: list[Candidate] = []
    for title, item_url, day_str in parse_list(decode_html(resp), resp.url):
        d = _parse_date(day_str)
        if not d or not is_same_project(cur_title, cur_purchaser or "中国银联", cur_date, title, "中国银联", d, cfg):
            continue
        kind = classify_kind(None, title)
        fetcher = (lambda u=item_url: _fetch_html_winners(session, u, "中国银联")) if kind == "result" else None
        out.append(Candidate(d.isoformat(), title, item_url, kind, "中国银联", detail_fetcher=fetcher))
    return out


def _collect_smg(session, cur_title: str, cur_purchaser: str | None, cur_date: date, since_3y: date, cfg: Config, cache: dict) -> list[Candidate]:
    list_url = "https://caigou.smg.cn/review/ann/index.html"
    if "smg_items" not in cache:
        resp = session.get(list_url)
        resp.raise_for_status()
        cache["smg_items"] = parse_list(decode_html(resp), list_url, stop_before=since_3y.isoformat())
    out: list[Candidate] = []
    for title, item_url, day_str in cache["smg_items"]:
        d = _parse_date(day_str)
        if not d or d < since_3y:
            continue
        if not is_same_project(cur_title, cur_purchaser, cur_date, title, "上海广播电视台（SMG）", d, cfg):
            continue
        kind = classify_kind(None, title)
        fetcher = (lambda u=item_url: _fetch_html_winners(session, u, cur_purchaser)) if kind == "result" else None
        out.append(Candidate(d.isoformat(), title, item_url, kind, "上海广播电视台（SMG）", detail_fetcher=fetcher))
    return out


def _collect_sitc(session, cur_title: str, cur_purchaser: str | None, cur_date: date, since_3y: date, cfg: Config) -> list[Candidate]:
    kws = _search_keywords(cur_title, None)
    if not kws:
        return []
    out: list[Candidate] = []
    for kw in kws[:1]:
        for type_no, template_id, default_kind in reversed(orgs.SITC_TYPES):  # 先查结果公告(type=3)
            url = f"{orgs.SITC_API}?method=getNotice&type={type_no}&templateId={template_id}&showNotice=all&seacrhVal={kw}"
            resp = session.post(url, headers={"Referer": orgs.SITC_PAGE})
            resp.raise_for_status()
            for rec in (resp.json().get("data") or []):
                title = (rec.get("fdSubject") or "").strip()
                fd_id = str(rec.get("fdId") or "").strip()
                d = _parse_date(rec.get("publishTime") or rec.get("time"))
                if not title or not fd_id or not d or d < since_3y:
                    continue
                if not is_same_project(cur_title, cur_purchaser, cur_date, title, None, d, cfg):
                    continue
                kind = "result" if default_kind == "result" else classify_kind(None, title)
                detail_url = f"{orgs.SITC_API}?method=view&fdId={fd_id}"
                fetcher = (lambda u=detail_url: _fetch_html_winners(session, u, cur_purchaser)) if kind == "result" else None
                out.append(Candidate(d.isoformat(), title, detail_url, kind, None, detail_fetcher=fetcher))
    return out


def _collect_source_specific(session, source: str, cur_title: str, cur_purchaser: str | None, cur_date: date, since_3y: date, cfg: Config, cache: dict) -> list[Candidate]:
    scope = f"{cur_title} {cur_purchaser or ''}"
    out: list[Candidate] = []
    if "银联" in scope and "银联商务" not in scope:
        out.extend(_collect_unionpay(session, cur_title, cur_purchaser, cur_date, since_3y, cfg))
    if any(w in scope for w in ("SMG", "广播电视台", "文广", "东方希杰")):
        out.extend(_collect_smg(session, cur_title, cur_purchaser, cur_date, since_3y, cfg, cache))
    if source == "ceair" or "东航" in scope or "东方航空" in scope:
        session.interval = max(session.interval, ceair.MIN_INTERVAL)
        for kw in _search_keywords(cur_title, None)[:1]:
            for rec in ceair._search(session, kw, since_3y, page_size=50, max_pages=1):
                title = (rec.get("title") or "").strip()
                d = _parse_date(rec.get("publishTime"))
                if d and is_same_project(cur_title, cur_purchaser, cur_date, title, "东方航空", d, cfg):
                    kind = classify_kind(None, title)
                    item_url = f"{ceair.BASE}/portal/portal/noticeDetails?id={rec['id']}"
                    out.append(Candidate(d.isoformat(), title, item_url, kind, "东方航空"))
    if source == "airport" or "机场" in scope:
        if not cache.get("airport_warmed"):
            session.get(airport.LIST_URL, headers={"Referer": airport.BASE + "/"})
            cache["airport_warmed"] = True
        for kw in _search_keywords(cur_title, None)[:1]:
            for rec in airport._search(session, kw, since_3y):
                n = airport._notice(rec)
                d = _parse_date(n.publish_date)
                if d and is_same_project(cur_title, cur_purchaser, cur_date, n.title, None, d, cfg):
                    out.append(Candidate(d.isoformat(), n.title, n.url, classify_kind(None, n.title)))
    if source == "gtht" or any(w in scope for w in ("国泰海通", "国泰君安", "海通证券")):
        session.mount("https://", LegacyTLSAdapter())
        for flag, kind in (("02", "result"), ("01", "tender")):
            for rec in gtht._search(session, flag, since_3y, "险"):
                title = (rec.get("title") or "").strip()
                d = _parse_date(rec.get("publishTime"))
                if d and is_same_project(cur_title, cur_purchaser, cur_date, title, "国泰海通证券", d, cfg):
                    url = f"{gtht.BASE}/#/procurnotice?id={rec['id']}&projectId={rec.get('projectId') or ''}&noticeFlag=true"
                    out.append(Candidate(d.isoformat(), title, url, kind, "国泰海通证券"))
    if source == "sse" or "证券交易所" in scope or "上交所" in scope:
        if "sse_notices" not in cache:
            sse_all = []
            for idx in range(6):
                page_url = sse.LIST_URL if idx == 0 else f"{sse.LIST_URL}s_list_{idx}.shtml"
                resp = session.get(page_url)
                if resp.status_code == 404:
                    break
                resp.raise_for_status()
                resp.encoding = "utf-8"
                sse_all.extend(sse._parse(resp.text))
            cache["sse_notices"] = sse_all
        for n in cache["sse_notices"]:
            d = _parse_date(n.publish_date)
            if d and d >= since_3y and is_same_project(cur_title, cur_purchaser, cur_date, n.title, "上海证券交易所", d, cfg):
                out.append(Candidate(d.isoformat(), n.title, n.url, classify_kind(None, n.title), "上海证券交易所"))
    return out


def _collect_ccgp(session, cur_title: str, cur_purchaser: str | None, cur_date: date, since_3y: date, cfg: Config, cache: dict) -> list[Candidate]:
    """在沪中央单位（消防、航道局、浦东干部学院等）只发中国政府采购网：仅在政采上海站未命中时低频补查 1 次。"""
    if cache.get("ccgp_rate_limited"):
        return []
    u = unit_anchor(cur_title, cur_purchaser)
    kws = _search_keywords(cur_title, cur_purchaser)
    kw = f"{u} 保险" if u else (kws[0] if kws else "")
    if not kw:
        return []
    session.interval = max(session.interval, 5.0)
    session.headers["Accept"] = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
    if not cache.get("ccgp_warmed"):
        ccgp._get(session, ccgp.HOME)
        time.sleep(3.0)
        cache["ccgp_warmed"] = True
    try:
        rows = ccgp._search(session, kw, "0", since_3y, max_pages=1)
    except ccgp.RateLimited as e:
        log.warning("中国政府采购网往年检索被限流，本轮后续跳过：%s", e)
        cache["ccgp_rate_limited"] = True
        return []
    out: list[Candidate] = []
    for n in rows:
        d = _parse_date(n.publish_date)
        if not d or not is_same_project(cur_title, cur_purchaser, cur_date, n.title, n.purchaser, d, cfg):
            continue
        kind = classify_kind(n.notice_type, n.title)
        fetcher = (lambda url=n.url, p=n.purchaser or cur_purchaser: _fetch_html_winners(session, url, p)) if kind == "result" else None
        out.append(Candidate(d.isoformat(), n.title, n.url, kind, n.purchaser, detail_fetcher=fetcher))
    return out


def pick_best(candidates: list[Candidate]) -> dict:
    """从候选历史公告中选出最有参考价值的一条（最新一轮优先；同轮内有中标结果的优先）。"""
    if not candidates:
        return {"status": "none"}
    candidates.sort(key=lambda c: (c.publish_date, 1 if c.kind == "result" else 0), reverse=True)
    newest_dt = _parse_date(candidates[0].publish_date)

    # 在最新一轮（与最新候选相差 180 天内）里优先找结果公告并提取中标公司
    same_round = [
        c for c in candidates
        if newest_dt and _parse_date(c.publish_date) and (newest_dt - _parse_date(c.publish_date)).days <= 180
    ]
    results = [c for c in same_round if c.kind == "result"]
    chosen = results[0] if results else same_round[0]

    for c in results:
        if not c.winners and c.detail_fetcher is not None:
            try:
                c.winners = c.detail_fetcher()
            except Exception as e:
                log.warning("读取往年结果详情失败（%s）：%s", c.url, e)
                c.winners = []
            c.detail_fetcher = None
        if c.winners:
            chosen = c
            break

    return {
        "status": "found",
        "date": chosen.publish_date[:10],
        "title": chosen.title,
        "url": chosen.url,
        "kind": chosen.kind,
        "winners": chosen.winners or [],
    }


def lookup_prior(row, cfg: Config, store, session, cache: dict | None = None) -> dict:
    """查询单条公告的往年发布及中标情况，返回可存入 extra['prior'] 的字典。"""
    title = row["title"]
    purchaser = row["purchaser"]
    source = row["source"]
    if is_one_off_project(title):
        return {"status": "one_off"}
    if not unit_anchor(title, purchaser):
        return {"status": "none"}

    cur_date = _parse_date(row["publish_date"]) or date.today()
    since_3y = cur_date - timedelta(days=LOOKBACK_YEARS_DAYS)
    cache = cache if cache is not None else {}

    memo_key = (unit_anchor(title, purchaser) or "", frozenset(insurance_buckets(title)), core_topic(title))
    if memo_key in cache:
        return cache[memo_key]

    candidates: list[Candidate] = []
    try:
        candidates.extend(_collect_local(store, title, purchaser, cur_date, since_3y, cfg, cache))
    except Exception as e:
        log.warning("本地往年检索异常（%s）：%s", title[:40], e)

    if session is not None:
        try:
            candidates.extend(_collect_source_specific(session, source, title, purchaser, cur_date, since_3y, cfg, cache))
        except Exception as e:
            log.warning("来源自有往年检索失败（%s）：%s", title[:40], e)

        if not any(c.winners or c.detail_fetcher for c in candidates):
            if source in ("zfcg_sh", "ccgp", "shggzy_zhcg", "orgs", "edu"):
                try:
                    candidates.extend(_collect_zfcg(session, title, purchaser, cur_date, since_3y, cfg))
                except Exception as e:
                    log.warning("上海政采往年检索失败（%s）：%s", title[:40], e)

        if not any(c.winners or c.detail_fetcher for c in candidates):
            if source in ("shggzy", "shggzy_zhcg", "shmetro", "chengtou", "orgs"):
                try:
                    candidates.extend(_collect_shggzy(session, title, purchaser, cur_date, since_3y, cfg))
                except Exception as e:
                    log.warning("国企采购往年检索失败（%s）：%s", title[:40], e)

        if not any(c.winners or c.detail_fetcher for c in candidates) and source == "orgs":
            try:
                candidates.extend(_collect_sitc(session, title, purchaser, cur_date, since_3y, cfg))
            except Exception as e:
                log.warning("国际招标往年检索失败（%s）：%s", title[:40], e)

        if not candidates and source == "ccgp":
            try:
                candidates.extend(_collect_ccgp(session, title, purchaser, cur_date, since_3y, cfg, cache))
            except Exception as e:
                log.warning("中国政采往年检索失败（%s）：%s", title[:40], e)

    result = pick_best(candidates)
    cache[memo_key] = result
    return result


def enrich_pending(rows: list, cfg: Config, store, session) -> list:
    """为尚未做过往年核查的待发信公告补充 extra['prior']，并返回刷新后的行列表。"""
    if not rows:
        return rows
    cache: dict = {}
    updated = False
    for r in rows:
        extra = json.loads(r["extra"] or "{}")
        if "prior" in extra:
            continue
        prior_info = lookup_prior(r, cfg, store, session, cache)
        extra["prior"] = prior_info
        store.update_extra(r["source"], r["source_id"], extra)
        updated = True
        status = prior_info.get("status")
        if status == "found":
            w = "、".join(prior_info.get("winners") or []) or "未查到公开中标人"
            log.info("往年核查 [%s] → %s %s（%s）", r["title"][:35], prior_info["date"], prior_info["title"][:30], w)
        elif status == "one_off":
            log.info("往年核查 [%s] → 一次性工程险，跳过", r["title"][:35])
        else:
            log.info("往年核查 [%s] → 近 3 年公开渠道未见同标", r["title"][:35])
    if not updated:
        return rows
    keys = {(r["source"], r["source_id"]) for r in rows}
    since = min((r["publish_date"][:10] for r in rows if r["publish_date"]), default="1970-01-01")
    return [r for r in store.pending(since) if (r["source"], r["source_id"]) in keys]


def format_prior_text(prior: dict | None) -> str:
    """格式化成纯文本（供 CSV 导出和日志使用）。"""
    if not prior:
        return ""
    status = prior.get("status")
    if status == "one_off":
        return "一次性工程险（跳过）"
    if status == "none":
        return "近3年未见同标"
    if status == "found":
        ym = (prior.get("date") or "")[:7]
        winners = prior.get("winners") or []
        if winners:
            return f"{ym} 发过｜原中标：{'、'.join(winners)}"
        return f"{ym} 发过（未查到公开中标人）"
    return ""
