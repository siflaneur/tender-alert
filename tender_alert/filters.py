import re
from datetime import date, timedelta

from .config import Config
from .models import Notice

# 名称里带「保险」但不是在采购保险的机构，匹配关键词前先去掉，例如：
#   中国大地财产保险股份有限公司…硬件项目招标公告（保险公司自己采购别的东西）
#   上海市医疗保险事务中心…硒鼓采购（医保部门采购办公用品）
# 前缀里不允许再出现「保险」，免得把「车辆保险采购（XX保险有限公司）」前面的真关键词一起吃掉。
_ORG_RE = re.compile(
    r"(?:(?!保险)[一-龥]){1,15}保险(?:股份|资产管理|经纪|代理|公估)?(?:股份)?有限(?:责任)?公司"
    r"|(?:(?!保险)[一-龥]){1,10}保险集团"
    # 医保/社保经办机构：前缀必须有，后缀紧跟，不能留空隙，
    # 否则「团体保险管理及理赔处理服务」里的「保险管理…处」也会被当成机构名去掉
    r"|(?:医疗|社会|养老|失业|工伤|生育)保险(?:事务|事业|基金|管理|监督)(?:管理|检查|服务)?(?:中心|局|所|处|站)(?![理置需属点])"
)


def in_window(notice: Notice, cfg: Config, today: date) -> bool:
    """是否在回看窗口内。日期解析不了的保留，宁可多提醒也别漏。"""
    try:
        published = date.fromisoformat(notice.publish_date[:10])
    except ValueError:
        return True
    return published >= today - timedelta(days=cfg.lookback_days)


def match(notice: Notice, cfg: Config, region_scoped: bool) -> list[str] | None:
    """命中返回命中的关键词列表，否则返回 None。

    region_scoped 表示来源本身只有上海的公告（如上海政府采购网），不用再按地区筛。
    """
    text = notice.title + " " + (notice.match_text or "")
    if any(w in text for w in cfg.exclude):
        return None
    stripped = _ORG_RE.sub(" ", text)
    hits = [k for k in cfg.keywords if k in stripped]
    if not hits:
        return None
    if notice.kind not in cfg.kinds:
        return None
    if not in_region(notice, cfg, region_scoped):
        return None
    return hits


def in_region(notice: Notice, cfg: Config, region_scoped: bool) -> bool:
    """是不是要关注的地区（上海）的公告。region_scoped 的来源抓回来的本来就都是上海的。"""
    if region_scoped:
        return True
    where = " ".join(filter(None, (notice.region, notice.purchaser, notice.title)))
    return any(r in where for r in cfg.regions)


def still_matches(row, cfg: Config) -> bool:
    """已存下但还没提醒的公告，按当前配置再筛一遍，让调过的类型、排除词、关键词立刻生效。"""
    if row["kind"] not in cfg.kinds:
        return False
    if any(w in row["title"] for w in cfg.exclude):
        return False
    return any(k in cfg.keywords for k in row["keywords"].split(","))
