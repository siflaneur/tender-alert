"""测试用：按录制顺序回放真实响应的 Session，不联网（样本由 --dry-run --record 录制）。"""

import base64
from urllib.parse import urlsplit

import requests


class ReplaySession:
    def __init__(self, fixture: dict):
        self.responses = fixture["responses"]
        self.pos = 0
        self.interval = 0
        self.headers: dict = {}
        self.robots = None

    def mount(self, prefix, adapter):
        pass

    def request(self, method, url, **kwargs):
        if self.pos >= len(self.responses):
            raise AssertionError(f"回放样本用完了，代码比录制时多发了请求：{method} {url}")
        rec = self.responses[self.pos]
        self.pos += 1
        want, got = urlsplit(rec["url"]), urlsplit(url)
        # 查询参数里有当天日期，每天都不一样，只核对方法、域名和路径
        if (rec["method"], want.netloc, want.path) != (method.upper(), got.netloc, got.path):
            raise AssertionError(f"第 {self.pos} 个请求和录制时不一样：录的是 {rec['method']} {rec['url']}，现在是 {method} {url}")
        resp = requests.Response()
        resp.status_code = rec["status"]
        resp._content = base64.b64decode(rec["body"])
        resp.headers["Content-Type"] = rec["content_type"]
        resp.url = rec["url"]
        resp.encoding = requests.utils.get_encoding_from_headers(resp.headers)  # 和真实请求的默认行为一致
        return resp

    def get(self, url, **kwargs):
        return self.request("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self.request("POST", url, **kwargs)
