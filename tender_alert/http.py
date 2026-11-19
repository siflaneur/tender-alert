import logging
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

import requests

log = logging.getLogger(__name__)

NETWORK_PROBE = "https://www.baidu.com/"


def wait_for_network(timeout: float = 180, interval: float = 15) -> bool:
    """等到能连上外网为止，最多等 timeout 秒。

    电脑刚开机或刚唤醒时，定时任务会立刻补跑，这时网络往往还没就绪，
    所有网站都会被拒绝连接；不等的话每个来源都会白白记一次失败。
    """
    deadline = time.monotonic() + timeout
    while True:
        try:
            requests.head(NETWORK_PROBE, timeout=5, headers={"User-Agent": USER_AGENT})
            return True
        except requests.RequestException as e:
            if time.monotonic() + interval > deadline:
                return False
            log.info("网络还没就绪（%s），%d 秒后再试", type(e).__name__, interval)
            time.sleep(interval)


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)


MAX_INTERVAL = 60.0  # 自动放慢后，同一网站的请求间隔最多放到 60 秒
MAX_RETRY_AFTER = 120.0  # 服务器要求等待的时间最多照做 2 分钟，再长就算失败，下次运行再说


def _retry_after(resp: requests.Response) -> float | None:
    """服务器在 Retry-After 里要求等多少秒（可能写秒数，也可能写一个时间点）。"""
    value = resp.headers.get("Retry-After")
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())


class PoliteSession(requests.Session):
    """对同一个网站限速的 Session：两次请求之间至少间隔 interval 秒，临时错误自动重试。

    网站返回 429（请求太多）或 503 时自动放慢：按 Retry-After 等待后重试，
    并把本次运行里对这个网站的请求间隔加倍（最多 60 秒）。
    给了 robots（robots.RobotsGuard）时，每个请求发出前先检查 robots.txt，被禁止就抛异常。
    """

    def __init__(self, interval: float = 2.0, retries: int = 2, timeout: float = 20.0, robots=None):
        super().__init__()
        self.robots = robots
        self.recorder = None  # recording.Recorder：录下收到的响应做测试样本
        self.interval = interval
        self.retries = retries
        self.timeout = timeout
        self._last: dict[str, float] = {}
        self._slow: dict[str, float] = {}  # 被限流后对各网站放慢到的间隔
        self.headers.update({
            "User-Agent": USER_AGENT,
            "Accept-Language": "zh-CN,zh;q=0.9",
        })

    def _slow_down(self, host: str) -> None:
        slower = min(max(self.interval, self._slow.get(host, 0.0)) * 2, MAX_INTERVAL)
        if slower > self._slow.get(host, 0.0):
            self._slow[host] = slower
            log.warning("%s 在限流，本次运行对它的请求间隔放慢到 %.0f 秒", host, slower)

    def request(self, method, url, **kwargs):
        kwargs.setdefault("timeout", self.timeout)
        full = requests.Request(method, url, params=kwargs.get("params")).prepare().url
        if self.robots is not None:
            self.robots.check(self, full)
        host = urlsplit(url).netloc
        for attempt in range(self.retries + 1):
            interval = max(self.interval, self._slow.get(host, 0.0))
            wait = self._last.get(host, 0) + interval - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            backoff = 5 * (attempt + 1)
            try:
                resp = super().request(method, url, **kwargs)
            except (requests.ConnectionError, requests.Timeout) as e:
                err = e
            else:
                if resp.status_code in (429, 503):
                    self._slow_down(host)
                    asked = _retry_after(resp)
                    if asked is not None and asked > MAX_RETRY_AFTER:
                        raise requests.HTTPError(
                            f"HTTP {resp.status_code}，网站要求 {asked:.0f} 秒后再来，本次放弃", response=resp)
                    backoff = asked if asked is not None else 10 * (attempt + 1)
                    err = requests.HTTPError(f"HTTP {resp.status_code}", response=resp)
                elif resp.status_code < 500:
                    if self.recorder is not None:
                        self.recorder.add(method, full, resp)
                    return resp
                else:
                    err = requests.HTTPError(f"HTTP {resp.status_code}", response=resp)
            finally:
                self._last[host] = time.monotonic()
            if attempt < self.retries:
                log.warning("请求失败，%.0f 秒后重试：%s %s（%s）", backoff, method, url, err)
                time.sleep(backoff)
        raise err
