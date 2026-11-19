"""数据来源。每个来源是一个模块，提供：

    NAME: str              显示名称，如「上海政府采购网」
    REGION_SCOPED: bool    True 表示抓回来的都是上海的公告，不用再按地区筛
    fetch(session, cfg, since) -> list[Notice]
                           抓取 since（date）及之后发布的公告

需要跨次运行记住状态的来源，再设 STATEFUL = True，fetch 多收 state（dict）和 dry_run 两个参数：
state 是上次存的内容，原地修改后，抓取成功时会自动存回数据库（dry-run 时不存）。

全量存档模式下抓「全部公告列表」的来源再设 INCREMENTAL = True：主流程传进来的 since 会改成
「上次成功运行的前一天」（不早于回看窗口），避免每次都把 30 天的全量重新翻一遍。
"""

import importlib
from types import ModuleType


def load_source(key: str) -> ModuleType:
    return importlib.import_module(f"{__name__}.{key}")


def source_display_name(key: str) -> str:
    """把来源 key（含 `orgs:unionpay` 这类子单位 key）转成可读名称。"""
    parent, sep, sub = key.partition(":")
    try:
        module = load_source(parent)
    except ImportError:
        return key
    if not sep:
        return module.NAME
    sub_fn = getattr(module, "sub_name", None)
    label = sub_fn(sub) if callable(sub_fn) else None
    return f"{module.NAME} · {label or sub}"
