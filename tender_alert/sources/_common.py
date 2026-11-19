"""几个全国性企业来源（东航、上海银行、国泰海通）共用的小工具。"""

import re
import ssl

from requests.adapters import HTTPAdapter

# 外地省市和大区名。不含「华东」（上海属于华东）
_REGIONS = (
    "北京|天津|重庆|河北|山西|内蒙古|辽宁|吉林|黑龙江|江苏|浙江|安徽|福建|江西|山东|河南|湖北|湖南|"
    "广东|广西|海南|四川|贵州|云南|西藏|陕西|甘肃|青海|宁夏|新疆|香港|澳门|台湾|"
    "南京|苏州|无锡|常州|南通|徐州|扬州|杭州|宁波|温州|绍兴|嘉兴|合肥|福州|厦门|南昌|济南|青岛|烟台|"
    "郑州|武汉|长沙|广州|深圳|东莞|佛山|南宁|海口|三亚|成都|贵阳|昆明|西安|兰州|西宁|银川|乌鲁木齐|"
    "哈尔滨|长春|沈阳|大连|石家庄|太原|呼和浩特|拉萨|大兴|西北|华北|东北|西南|华南|华中"
)
_BRANCH_RE = re.compile(rf"(?:{_REGIONS})(?:地区)?(?:分行|分公司|支行|基地|办事处)")
_REGION_RE = re.compile(_REGIONS)
_META_CHARSET_RE = re.compile(rb"<meta[^>]+charset\s*=\s*['\"]?\s*([a-zA-Z0-9._-]+)", re.I)


def decode_html(resp) -> str:
    """解码网页响应：响应头未声明编码时先读前 4KB 的 <meta charset> 或试 UTF-8，避免对大网页跑全文嗅探。"""
    enc = getattr(resp, "encoding", None)
    if enc and enc.lower() != "iso-8859-1":
        return resp.text
    raw = getattr(resp, "content", None)
    if isinstance(raw, (bytes, bytearray)):
        m = _META_CHARSET_RE.search(raw[:4096])
        if m:
            declared = m.group(1).decode("ascii", errors="ignore").lower()
            codec = "gb18030" if declared in ("gb2312", "gbk") else declared
            try:
                return raw.decode(codec)
            except (LookupError, UnicodeDecodeError):
                pass
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            pass
        fallback = getattr(resp, "apparent_encoding", None) or "gb18030"
        return raw.decode(fallback, errors="replace")
    resp.encoding = getattr(resp, "apparent_encoding", None) or "utf-8"
    return resp.text


def outside_shanghai(title: str) -> bool:
    """标题看起来是外地分支机构的采购。

    「天津分行」「西北分公司」这类一律算外地；否则标题带「上海」就算上海
    （上海很多路名是外地地名，如南京路、江苏路）；都没有时出现外地地名才算外地。
    """
    if _BRANCH_RE.search(title):
        return True
    if "上海" in title:
        return False
    return bool(_REGION_RE.search(title))


class LegacyTLSAdapter(HTTPAdapter):
    """有些银行、券商的服务器还在用旧式 TLS 重协商，OpenSSL 3 默认拒绝连接，这里只放开这一项。"""

    def init_poolmanager(self, *args, **kwargs):
        ctx = ssl.create_default_context()
        ctx.options |= getattr(ssl, "OP_LEGACY_SERVER_CONNECT", 0x4)
        kwargs["ssl_context"] = ctx
        return super().init_poolmanager(*args, **kwargs)
