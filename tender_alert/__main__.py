import argparse
import contextlib
import copy
import logging
import os
import sys
from datetime import date, datetime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .config import ROOT, load_config, load_mail_settings
from .export import export_archive_csv, export_csv, export_json
from .filters import in_region, in_window, match, still_matches
from .http import PoliteSession, wait_for_network
from .mailer import build_html, send
from .models import classify_kind
from .prior import enrich_pending
from .recording import Recorder
from .robots import RobotsGuard
from .sources import load_source, source_display_name
from .store import Store

log = logging.getLogger("tender_alert")

DB_PATH = ROOT / "data" / "tender-alert.db"
LOCK_PATH = ROOT / "data" / "run.lock"
HISTORY_CSV = ROOT / "exports" / "历史命中公告.csv"
HISTORY_JSON = ROOT / "exports" / "历史命中公告.json"
ARCHIVE_DIR = ROOT / "exports" / "全量存档"
FIXTURE_DIR = ROOT / "tests" / "fixtures"
LOG_MAX_BYTES = 5 * 1024 * 1024  # 单个日志最多 5MB，自动轮转保留最近 3 份
LOG_BACKUP_COUNT = 3
MAX_PREVIEWS = 20  # previews/ 最多保留最近 20 个网页预览


def setup_logging(verbose: bool) -> None:
    (ROOT / "logs").mkdir(exist_ok=True)
    handlers: list[logging.Handler] = [
        RotatingFileHandler(
            ROOT / "logs" / "tender-alert.log",
            maxBytes=LOG_MAX_BYTES,
            backupCount=LOG_BACKUP_COUNT,
            encoding="utf-8",
        ),
    ]
    if sys.stdout is not None:  # 用 pythonw 定时运行时没有控制台
        handlers.append(logging.StreamHandler(sys.stdout))
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
    )


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="tender_alert", description="上海保险招标提醒")
    p.add_argument("--dry-run", action="store_true", help="只生成邮件预览，不发送，也不改动数据库")
    p.add_argument("--sources", nargs="+", help="只跑指定来源，空格或逗号分隔，如 zfcg_sh ccgp")
    p.add_argument("--days", type=int, help="临时覆盖 config.toml 里的 lookback_days")
    p.add_argument("--test-mail", action="store_true", help="发一封测试邮件，检查邮箱配置")
    p.add_argument("--export-json", action="store_true", help=f"把历史筛中结果导出成 JSON（{HISTORY_JSON.relative_to(ROOT)}），不抓取")
    p.add_argument("--export-archive", action="store_true", help=f"把全量存档按月重新导出成 CSV（{ARCHIVE_DIR.relative_to(ROOT)}），不抓取")
    p.add_argument("--record", action="store_true",
                   help=f"和 --dry-run 一起用：把各来源收到的真实响应录成测试样本（{FIXTURE_DIR.relative_to(ROOT)}）")
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args()


def write_preview(subject: str, html: str, preview_dir: Path | None = None) -> Path:
    out_dir = preview_dir or (ROOT / "previews")
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{datetime.now():%Y%m%d-%H%M%S}.html"
    path.write_text(f"<html><head><meta charset='utf-8'><title>{subject}</title></head><body>{html}</body></html>", encoding="utf-8")
    old = sorted(out_dir.glob("*.html"), key=lambda p: (p.stat().st_mtime, p.name))[:-MAX_PREVIEWS]
    for p in old:
        p.unlink(missing_ok=True)
    return path


@contextlib.contextmanager
def single_instance_lock(lock_path: Path = LOCK_PATH):
    """正式运行时的单实例文件互斥锁，防止手动运行与后台定时任务撞车导致锁库或重复发信。

    进程退出（含意外崩溃或被强杀）后操作系统会自动释放底层文件锁，不会留下死锁文件。
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    f = open(lock_path, "a+b")
    locked = False
    try:
        f.seek(0)
        if os.name == "nt":
            import msvcrt
            try:
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                locked = True
            except OSError:
                locked = False
        else:
            import fcntl
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
            except OSError:
                locked = False
        yield locked
    finally:
        if locked:
            try:
                if os.name == "nt":
                    import msvcrt
                    f.seek(0)
                    msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        f.close()


def already_done_today(key: str, cfg, store: Store, args: argparse.Namespace, today: date) -> bool:
    """更新慢的来源（config.toml 的 once_a_day）当天已经抓成功过，这次就跳过。

    当天还没成功（电脑没开、上一次失败）时照常抓，所以早上那次失败了，下午会自动补上。
    用 --sources 点名要跑、或者在录测试样本时，不跳过。
    """
    if key not in cfg.once_a_day or args.sources or args.record:
        return False
    return store.last_success_date(key) == today


def run(args: argparse.Namespace) -> int:
    cfg = load_config()
    mail = load_mail_settings()
    if args.days is not None:
        cfg.lookback_days = args.days

    if args.test_mail:
        if not mail.configured:
            log.error("邮箱还没配置：复制 .env.example 为 .env 并填写")
            return 1
        send(mail, "招标提醒：测试邮件", "<p>收到这封邮件说明邮箱配置正确。</p>")
        log.info("测试邮件已发送到 %s", ", ".join(mail.to))
        return 0

    if not wait_for_network():
        # 网络不通时所有来源都会失败，不应计入各来源的连续失败次数，否则会误发告警
        log.error("等了 3 分钟仍连不上外网，本次运行跳过，不计入任何来源的失败次数")
        return 1

    if args.record and not args.dry_run:
        log.error("--record 只能和 --dry-run 一起用，免得录样本时改动数据库或发信")
        return 2

    keys = [k for v in args.sources for k in v.split(",") if k] if args.sources else cfg.sources
    today = date.today()
    since = today - timedelta(days=cfg.lookback_days)
    store = Store(DB_PATH, scratch=args.dry_run)
    source_names: dict[str, str] = {}

    report: list[dict] = []  # 每个来源本次的运行情况，附在邮件末尾
    robots = RobotsGuard(ROOT / "data" / "robots_cache.json")  # 所有请求都先过 robots.txt 检查
    for key in keys:
        if already_done_today(key, cfg, store, args, today):
            try:
                source_names[key] = load_source(key).NAME
            except ImportError:
                source_names[key] = key
            log.info("%s：每天一次的来源，今天已经抓过，跳过", source_names[key])
            report.append({"name": source_names[key], "skipped": True})
            continue
        session = PoliteSession(interval=cfg.request_interval, robots=robots)
        state = store.get_state(key)
        if args.record:
            session.recorder = Recorder()
            state_before = copy.deepcopy(state)  # 回放时要用抓取前的状态
        try:
            module = load_source(key)
            source_names[key] = module.NAME
            src_since = since
            if cfg.archive and getattr(module, "INCREMENTAL", False):
                # 抓全量列表的来源每次只从上次成功的前一天抓起：更早的已经存档、也做过保险筛选
                last = store.last_success_date(key)
                if last and last - timedelta(days=1) > since:
                    src_since = last - timedelta(days=1)
            if getattr(module, "STATEFUL", False):
                notices = module.fetch(session, cfg, src_since, state, dry_run=args.dry_run)
            else:
                notices = module.fetch(session, cfg, src_since)
        except Exception as e:
            failures = store.record_failure(key, f"{type(e).__name__}: {e}")
            log.exception("%s 抓取失败（连续第 %d 次）", source_names.get(key, key), failures)
            report.append({"name": source_names.get(key, key), "error": f"{type(e).__name__}: {e}"[:120],
                           "failures": failures})
            continue
        if args.record and session.recorder.responses:
            session.recorder.save(FIXTURE_DIR / f"{key}.json.gz", source=key, since=src_since.isoformat(),
                                  archive=cfg.archive, state=state_before)
            log.info("%s：已录下 %d 个响应做测试样本", module.NAME, len(session.recorder.responses))

        new = archived = 0
        with store.batch():
            store.record_success(key)
            for sub_key, (sub_label, sub_err) in getattr(session, "sub_health", {}).items():
                source_names[sub_key] = f"{module.NAME} · {sub_label}"
                if sub_err is None:
                    store.record_success(sub_key)
                else:
                    sub_failures = store.record_failure(sub_key, sub_err)
                    log.info("%s 连续第 %d 次抓取失败", source_names[sub_key], sub_failures)
            for n in notices:
                if n.kind == "other":
                    n.kind = classify_kind(n.notice_type, n.title)
                if cfg.archive and in_region(n, cfg, module.REGION_SCOPED):
                    archived += store.archive(n)
                if not in_window(n, cfg, today):
                    continue
                hits = match(n, cfg, module.REGION_SCOPED)
                if not hits:
                    log.debug("未命中 [%s] %s %s", n.kind, n.publish_date[:10], n.title)  # 用 -v 查看，方便调关键词
                elif store.add(n, hits, cfg.sources):
                    new += 1
            if getattr(module, "STATEFUL", False):
                store.set_state(key, state)
        log.info("%s：抓到 %d 条，新命中 %d 条%s", module.NAME, len(notices), new,
                 f"，新存档 {archived} 条" if cfg.archive else "")
        report.append({"name": module.NAME, "fetched": len(notices), "new": new,
                       "archived": archived if cfg.archive else None})

    rows = [r for r in store.pending(since.isoformat()) if still_matches(r, cfg)]
    if rows and not args.record:
        prior_session = PoliteSession(interval=cfg.request_interval, robots=robots)
        rows = [r for r in enrich_pending(rows, cfg, store, prior_session) if still_matches(r, cfg)]
    failing = [
        f for f in store.unalerted_failures(cfg.failure_threshold)
        if f["source"].partition(":")[0] in keys
    ]
    if not rows:
        log.info("没有新的上海保险类公告")  # 照样发一封运行报告（用户 2026-11-02 要求每次都发）

    # 邮件里显示来源名称，包括这次没跑但之前存下来的来源（含 orgs:xxx / edu:xxx 子单位）
    for key in {r["source"] for r in rows} | {f["source"] for f in failing}:
        if key not in source_names:
            source_names[key] = source_display_name(key)
    warnings = [
        f"{source_names[f['source']]} 已连续 {f['consecutive_failures']} 次抓取失败：{f['last_error']}"
        for f in failing
    ]
    total_archived = sum(r.get("archived") or 0 for r in report)
    if rows:
        subject = f"上海保险招标提醒：{len(rows)} 条新公告"
    elif warnings:
        subject = "招标提醒：数据来源异常"
    else:
        subject = "招标提醒：本次无新保险公告" + (f"（新存档 {total_archived} 条）" if cfg.archive else "")
    html = build_html(rows, warnings, source_names, report)

    if args.dry_run or not mail.configured:
        path = write_preview(subject, html)
        reason = "--dry-run" if args.dry_run else "邮箱未配置"
        log.info("%s，未发送。邮件预览：%s", reason, path)
        return 0

    try:
        send(mail, subject, html)
    except Exception:
        path = write_preview(subject, html)
        log.exception("邮件发送失败，下次运行会重试。邮件预览：%s", path)
        return 1
    store.mark_notified(rows)
    store.mark_alerted([f["source"] for f in failing])
    log.info("已发送邮件到 %s：%s", ", ".join(mail.to), subject)
    return 0


def update_history_csv() -> None:
    """正式运行后刷新历史结果 CSV。导出失败只记日志，不影响本次运行结果。"""
    if not DB_PATH.exists():
        return
    try:
        count = export_csv(DB_PATH, HISTORY_CSV)
    except PermissionError:
        log.warning("%s 正被别的程序（多半是 Excel）打开，没能更新，关掉后下次运行会更新", HISTORY_CSV.name)
    except Exception:
        log.exception("更新历史结果 CSV 失败")
    else:
        log.info("历史结果已更新：%s（%d 条）", HISTORY_CSV, count)


def update_archive_csv(touched_since: str | None) -> None:
    """正式运行后刷新全量存档的月度 CSV，只重写这次有变动的月份。失败只记日志。"""
    if not DB_PATH.exists():
        return
    try:
        written = export_archive_csv(DB_PATH, ARCHIVE_DIR, touched_since)
    except Exception:
        log.exception("更新全量存档 CSV 失败")
    else:
        if written:
            log.info("全量存档已导出：%s（%s）", ARCHIVE_DIR,
                     "，".join(f"{m} {n} 条" for m, n in written.items()))


def main() -> int:
    if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = parse_args()
    setup_logging(args.verbose)
    if args.export_archive:
        try:
            written = export_archive_csv(DB_PATH, ARCHIVE_DIR)
            log.info("已导出全量存档 %d 个月、共 %d 条：%s", len(written), sum(written.values()), ARCHIVE_DIR)
            return 0
        except Exception:
            log.exception("导出全量存档失败")
            return 1
    if args.export_json:
        try:
            count = export_json(DB_PATH, HISTORY_JSON)
            log.info("已导出 %d 条历史结果：%s", count, HISTORY_JSON)
            return 0
        except Exception:
            log.exception("导出历史 JSON 失败")
            return 1

    need_lock = not args.dry_run and not args.test_mail
    lock_ctx = single_instance_lock(LOCK_PATH) if need_lock else contextlib.nullcontext(True)
    with lock_ctx as acquired:
        if not acquired:
            log.warning("已有另一个 tender-alert 实例正在正式运行，本次跳过避免锁库或重复发信")
            return 0
        started = datetime.now().isoformat(timespec="seconds")  # 只重新导出这次运行动过的月份
        try:
            code = run(args)
        except Exception:
            # 定时运行时看不到控制台，所有意外错误都要进日志文件
            log.exception("运行失败")
            code = 1
        if need_lock:
            update_history_csv()
            update_archive_csv(started)  # 没开存档时本次没有存档变动，什么也不写
        return code


if __name__ == "__main__":
    sys.exit(main())
