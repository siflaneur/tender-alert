from dataclasses import dataclass, field


@dataclass
class Notice:
    """一条招标公告，各来源抓到的数据统一成这个格式。"""

    source: str  # 来源 key，如 zfcg_sh
    source_id: str  # 来源内的唯一 id，用于去重
    title: str
    url: str  # 详情页地址，浏览器能直接打开
    publish_date: str  # YYYY-MM-DD
    purchaser: str | None = None
    notice_type: str | None = None  # 来源给出的原始类型名，如「公开招标公告」
    region: str | None = None
    kind: str = "other"  # 归一化后的类型，见 classify_kind
    match_text: str | None = None  # 标题之外也参与关键词匹配的文字，如采购意向的正文摘要
    extra: dict = field(default_factory=dict)  # budget 预算、deadline 截止/开标时间，会显示在邮件里


# 按顺序匹配，先命中的生效：「XX招标公告（更正）」应归为更正而不是招标
_KIND_RULES = [
    ("intent", ("意向", "招标计划", "采购计划")),
    ("correction", ("更正", "变更", "澄清", "延期", "补充公告")),
    ("result", ("中标", "成交", "结果", "废标", "流标", "终止", "失败", "异常", "合同公告", "验收")),
    ("tender", ("招标", "采购公告", "磋商", "谈判", "询价", "单一来源", "竞价", "比选", "邀请", "资格预审", "征集")),
]


def classify_kind(notice_type: str | None, title: str) -> str:
    """把各网站五花八门的公告类型归成 tender / correction / result / intent / other。"""
    for text in (notice_type or "", title):
        for kind, words in _KIND_RULES:
            if any(w in text for w in words):
                return kind
    return "other"
