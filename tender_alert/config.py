import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent

ALL_KINDS = ["tender", "correction", "result", "intent", "other"]


@dataclass
class Config:
    keywords: list[str]
    exclude: list[str]
    regions: list[str]
    kinds: list[str]
    lookback_days: int
    sources: list[str]
    request_interval: float
    failure_threshold: int
    archive: bool = False  # 全量存档：抓到的所有上海公告都存下来，不限保险、不限类型
    once_a_day: tuple[str, ...] = ()  # 更新慢的来源：当天抓成功过一次就不再抓

    @property
    def fetch_kinds(self) -> list[str]:
        """来源按这个决定抓哪些公告类型：开了全量存档就抓全部类型，否则只抓要提醒的类型。"""
        return ALL_KINDS if self.archive else self.kinds


@dataclass
class MailSettings:
    host: str
    port: int
    user: str
    password: str
    to: list[str]

    @property
    def configured(self) -> bool:
        return bool(self.host and self.user and self.password and self.to)


def load_config(path: Path = ROOT / "config.toml") -> Config:
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    flt, src, alert = raw["filter"], raw["sources"], raw.get("alert", {})
    return Config(
        keywords=flt["keywords"],
        exclude=flt.get("exclude", []),
        regions=flt.get("regions", ["上海"]),
        kinds=flt.get("kinds", ["tender", "correction", "other"]),
        lookback_days=int(flt.get("lookback_days", 7)),
        sources=src["enabled"],
        request_interval=float(src.get("request_interval", 2.0)),
        failure_threshold=int(alert.get("failure_threshold", 3)),
        archive=bool(raw.get("archive", {}).get("enabled", False)),
        once_a_day=tuple(src.get("once_a_day", [])),
    )


def load_mail_settings() -> MailSettings:
    load_dotenv(ROOT / ".env")
    return MailSettings(
        host=os.getenv("SMTP_HOST", "").strip(),
        port=int(os.getenv("SMTP_PORT", "465") or 465),
        user=os.getenv("SMTP_USER", "").strip(),
        password=os.getenv("SMTP_PASSWORD", "").strip(),
        to=[a.strip() for a in os.getenv("MAIL_TO", "").split(",") if a.strip()],
    )
