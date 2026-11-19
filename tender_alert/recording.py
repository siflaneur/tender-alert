"""把来源收到的真实响应录下来，存成测试样本（借鉴 Scrapling 的开发模式缓存思路）。

用法：`.\\run.ps1 --dry-run --record`，每个来源抓取成功后，它收到的所有响应按顺序存到
tests/fixtures/<来源>.json.gz。测试里用 tests/replay.py 的 ReplaySession 按顺序回放，不联网。
网站改版或请求顺序变了，回放测试会失败，提示重新录制。
"""

import base64
import gzip
import json
from datetime import datetime
from pathlib import Path

import requests


class Recorder:
    def __init__(self):
        self.responses: list[dict] = []

    def add(self, method: str, url: str, resp: requests.Response) -> None:
        self.responses.append({
            "method": method.upper(),
            "url": url,
            "status": resp.status_code,
            "content_type": resp.headers.get("Content-Type", ""),
            "body": base64.b64encode(resp.content).decode("ascii"),
        })

    def save(self, path: Path, **meta) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {**meta, "recorded_at": datetime.now().isoformat(timespec="seconds"), "responses": self.responses}
        with gzip.open(path, "wt", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)


def load(path: Path) -> dict:
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return json.load(f)
