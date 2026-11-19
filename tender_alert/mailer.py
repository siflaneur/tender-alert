import json
import smtplib
import sqlite3
from datetime import datetime
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr
from html import escape

from .config import MailSettings

KIND_LABELS = {
    "tender": "招标/采购",
    "correction": "更正/变更",
    "result": "结果",
    "intent": "采购意向",
    "other": "其他",
}


def build_html(rows: list[sqlite3.Row], warnings: list[str], source_names: dict[str, str],
               report: list[dict] | None = None) -> str:
    parts = [
        "<div style=\"font-family:'Microsoft YaHei',sans-serif;font-size:14px;color:#222\">",
    ]
    if warnings:
        parts.append(
            '<div style="background:#fff4e5;border:1px solid #f0b46c;padding:10px 14px;margin-bottom:16px">'
            "<b>⚠ 数据来源异常</b><ul style=\"margin:6px 0 0 0;padding-left:20px\">"
        )
        parts += [f"<li>{escape(w)}</li>" for w in warnings]
        parts.append("</ul></div>")
    if rows:
        parts.append(f"<p>本次发现 <b>{len(rows)}</b> 条新的上海保险类招标公告：</p>")
        parts.append(
            '<table cellpadding="6" style="border-collapse:collapse;width:100%;font-size:13px">'
            '<tr style="background:#f2f4f7;text-align:left">'
            "<th>发布日期</th><th>类型</th><th>标题</th><th>采购单位</th><th>来源</th></tr>"
        )
        for r in rows:
            extra = json.loads(r["extra"] or "{}")
            deadline = extra.get("deadline")
            if deadline and deadline < datetime.now().strftime("%Y-%m-%d %H:%M"):
                deadline += "（已截止）"
            details = [
                f"预算 {extra['budget']}" if extra.get("budget") else None,
                f"截止/开标 {deadline}" if deadline else None,
                f"命中：{r['keywords']}",
            ]
            parts.append(
                '<tr style="border-top:1px solid #e5e7eb;vertical-align:top">'
                f"<td style=\"white-space:nowrap\">{escape(r['publish_date'][:10] or '未知')}</td>"
                f"<td style=\"white-space:nowrap\">{escape(KIND_LABELS.get(r['kind'], r['kind']))}</td>"
                f"<td><a href=\"{escape(r['url'], quote=True)}\">{escape(r['title'])}</a>"
                f"{_prior_html(extra.get('prior'))}"
                f"<br><span style=\"display:inline-block;margin-top:4px;color:#888;font-size:12px\">{escape(' · '.join(filter(None, details)))}</span></td>"
                f"<td>{escape(r['purchaser'] or '—')}</td>"
                f"<td style=\"white-space:nowrap\">{escape(source_names.get(r['source'], r['source']))}</td>"
                "</tr>"
            )
        parts.append("</table>")
    if not rows:
        parts.append("<p>本次没有新的上海保险类公告。</p>")
    if report:
        parts.append(_report_html(report))
    parts.append('<p style="color:#888;font-size:12px">由 tender-alert 自动发送。</p></div>')
    return "\n".join(parts)


def _prior_html(prior: dict | None) -> str:
    """单条公告标题下方的往年续标与曾经中标公司标记。"""
    if not prior:
        return ""
    status = prior.get("status")
    if status == "one_off":
        return (
            '<br><span style="display:inline-block;margin-top:4px;padding:1px 7px;'
            'background:#fef3c7;color:#92400e;border-radius:3px;font-size:12px">'
            "一次性工程险（跳过往年核查）</span>"
        )
    if status == "none":
        return (
            '<br><span style="display:inline-block;margin-top:4px;color:#888;font-size:12px">'
            "往年核查：近 3 年公开渠道未见同标</span>"
        )
    if status == "found":
        ym = escape((prior.get("date") or "")[:7] or "往年")
        url = prior.get("url") or ""
        link = f' · <a href="{escape(url, quote=True)}" style="color:inherit">往年公告</a>' if url else ""
        winners = prior.get("winners") or []
        if winners:
            who = escape("、".join(winners))
            return (
                '<br><span style="display:inline-block;margin-top:4px;padding:2px 7px;'
                'background:#eef6ff;color:#1e40af;border-radius:3px;font-size:12px">'
                f"★ 往年续标（{ym}）· 曾经中标：<b>{who}</b>{link}</span>"
            )
        return (
            '<br><span style="display:inline-block;margin-top:4px;padding:2px 7px;'
            'background:#f3f4f6;color:#374151;border-radius:3px;font-size:12px">'
            f"↻ 往年发过（{ym}，未查到公开中标公司）{link}</span>"
        )
    return ""


def _report_html(report: list[dict]) -> str:
    """本次运行情况：只写一行汇总，不逐个列来源；只有失败的来源才单独列出原因。"""
    show_archive = any(r.get("archived") is not None for r in report)
    total = sum(r.get("archived") or 0 for r in report)
    summary = f"，共新存档 {total} 条" if show_archive else ""
    failures = [r for r in report if "error" in r]
    skipped = sum(1 for r in report if r.get("skipped"))
    parts = [
        f'<p style="margin-top:20px;color:#555;font-size:12px"><b>本次运行情况</b>：{len(report)} 个来源，'
        f'{len(report) - len(failures) - skipped} 个正常'
        f'{f"、{len(failures)} 个失败" if failures else ""}{f"、{skipped} 个今天已抓过" if skipped else ""}{summary}。</p>'
    ]
    if failures:
        parts.append('<ul style="margin:0;padding-left:20px;font-size:12px;color:#c0392b">')
        parts += [f"<li>{escape(r['name'])}：失败（连续第 {r['failures']} 次）：{escape(r['error'])}</li>"
                  for r in failures]
        parts.append("</ul>")
    return "".join(parts)


def send(settings: MailSettings, subject: str, html: str) -> None:
    """发送邮件。sendmail 成功返回就算发出去了，之后断开连接时的错误一律忽略。

    QQ 邮箱等服务器对 QUIT 的回复不规范，smtplib 会在信已送达后再抛异常；
    如果把它当成发送失败，下次运行就会把同一批公告再发一遍。
    """
    msg = MIMEText(html, "html", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = formataddr(("招标提醒", settings.user))
    msg["To"] = ", ".join(settings.to)
    if settings.port == 465:
        server = smtplib.SMTP_SSL(settings.host, settings.port, timeout=30)
    else:
        server = smtplib.SMTP(settings.host, settings.port, timeout=30)
    try:
        if settings.port != 465:
            server.starttls()
        server.login(settings.user, settings.password)
        server.sendmail(settings.user, settings.to, msg.as_string())
    finally:
        try:
            server.quit()
        except (smtplib.SMTPException, OSError):
            server.close()
