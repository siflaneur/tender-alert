"""在代码里强制遵守 robots.txt：每个请求发出前先检查路径，被禁止就报错，请求不会发出去。

解析规则参照 RFC 9309（Google 的实现也一样）：
- 只看 `User-agent: *` 那一组，我们没有专用的爬虫名
- 路径支持 `*` 通配和 `$` 结尾；Allow 和 Disallow 都匹配时取最长的那条，一样长取 Allow
- robots.txt 返回 4xx，或者返回的是网页而不是规则文件：视为没有限制
- 取不到（网络错误、5xx）：本次放行但不缓存，记一条警告，下次运行再取
结果缓存在 data/robots_cache.json，7 天内不重复请求（有的站搜索接口限流敏感，能少打一次是一次）。

用户授权的例外写在 EXCEPTIONS 里。**新增或放宽例外必须由用户本人确认**，agent 不能自己加。
"""

import json
import logging
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

import requests

log = logging.getLogger(__name__)

CACHE_DAYS = 7

# (域名, 路径前缀, 授权说明)
EXCEPTIONS = [
    ("www.shggzy.com", "/search/",
     "综合采购的一次性历史补抓，用户 2026-10-30 确认；只在 source_state 里没有 backfilled 时用一次"),
    ("eps.shmetro.com", "/",
     "申通地铁整站 Disallow，用户 2026-10-31 授权、2026-11-02 再次确认；实际用无头浏览器打开公开首页"),
]


class RobotsDisallowed(RuntimeError):
    pass


def parse(text: str) -> list[tuple[bool, str]]:
    """取出 `User-agent: *` 组里的规则：[(是否 Allow, 路径模式), ...]。"""
    rules: list[tuple[bool, str]] = []
    agents: list[str] = []
    in_rules = False  # 读到规则行后，再遇到 User-agent 就是新的一组
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if ":" not in line:
            continue
        field, value = (s.strip() for s in line.split(":", 1))
        field = field.lower()
        if field == "user-agent":
            if in_rules:
                agents, in_rules = [], False
            agents.append(value.lower())
        elif field in ("allow", "disallow"):
            in_rules = True
            if "*" in agents and value:
                rules.append((field == "allow", value))
    return rules


def _pattern(rule: str) -> re.Pattern:
    anchored = rule.endswith("$")
    body = re.escape(rule[:-1] if anchored else rule).replace(r"\*", ".*")
    return re.compile(body + ("$" if anchored else ""))


def allowed(rules: list[tuple[bool, str]], path: str) -> tuple[bool, str | None]:
    """path 含查询串。返回 (是否允许, 起决定作用的规则)。"""
    best: tuple[int, bool, str] | None = None
    for allow, rule in rules:
        if _pattern(rule).match(path):
            key = (len(rule), allow)
            if best is None or key > best[:2]:
                best = (len(rule), allow, rule)
    if best is None:
        return True, None
    return best[1], best[2]


def _looks_like_rules(resp: requests.Response) -> bool:
    text = resp.text.lstrip()
    return not text.startswith("<") and re.search(r"(?im)^\s*user-agent\s*:", text) is not None


class RobotsGuard:
    def __init__(self, cache_path: Path):
        self.cache_path = cache_path
        try:
            self.cache: dict = json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.cache = {}
        self._unreachable: set[str] = set()  # 本次运行取不到 robots.txt 的网站，不重复取、不写进缓存

    def check(self, session: requests.Session, url: str) -> None:
        """url 被 robots.txt 禁止时抛 RobotsDisallowed。session 用来取 robots.txt（带上来源的 TLS 设置）。"""
        parts = urlsplit(url)
        path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        for host, prefix, _why in EXCEPTIONS:
            if parts.netloc == host and path.startswith(prefix):
                return
        origin = f"{parts.scheme}://{parts.netloc}"
        ok, rule = allowed(self._rules(session, origin), path)
        if not ok:
            raise RobotsDisallowed(f"{origin}/robots.txt 禁止抓取 {path}（规则 Disallow: {rule}），请求没有发出")

    def _rules(self, session: requests.Session, origin: str) -> list[tuple[bool, str]]:
        if origin in self._unreachable:
            return []
        entry = self.cache.get(origin)
        if entry and datetime.fromisoformat(entry["fetched"]) > datetime.now() - timedelta(days=CACHE_DAYS):
            return [tuple(r) for r in entry["rules"]]
        try:
            # 直接用 requests 的原始方法，不经过 PoliteSession 的检查和重试
            resp = requests.Session.request(session, "GET", origin + "/robots.txt", timeout=10)
        except requests.RequestException as e:
            log.warning("取不到 %s/robots.txt（%s），本次放行", origin, type(e).__name__)
            self._unreachable.add(origin)
            return []
        finally:
            if hasattr(session, "_last"):  # 算作对这个网站的一次请求，后面的正式请求照样要隔开
                session._last[urlsplit(origin).netloc] = time.monotonic()
        if resp.status_code >= 500:
            log.warning("%s/robots.txt 返回 %d，本次放行", origin, resp.status_code)
            self._unreachable.add(origin)
            return []
        resp.encoding = resp.encoding or "utf-8"
        rules = parse(resp.text) if resp.status_code < 400 and _looks_like_rules(resp) else []
        self.cache[origin] = {"fetched": datetime.now().isoformat(timespec="seconds"), "rules": rules}
        self._save()
        return rules

    def _save(self) -> None:
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(json.dumps(self.cache, ensure_ascii=False, indent=1), encoding="utf-8")
        except OSError as e:
            log.warning("保存 robots.txt 缓存失败：%s", e)
