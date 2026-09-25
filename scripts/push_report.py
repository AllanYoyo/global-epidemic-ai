#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把当日政策监测日报要点推送到办公渠道: 企业微信群机器人 / 钉钉机器人 / 邮箱。

配了哪个渠道的环境变量就发哪个;都未配置则提示并退出(码 1)。--dry-run 只打印不发送。
邮件发送(Tuta 网页邮箱)不经本脚本: 由 Hermes 直接调用其 tuta-webmail 技能完成。

环境变量:
  WECHAT_WEBHOOK       企业微信群机器人 Webhook 地址
  DINGTALK_WEBHOOK     钉钉自定义机器人 Webhook 地址
  DINGTALK_SECRET      钉钉加签密钥(可选, 安全设置选"加签"时必填)
  SMTP_HOST / SMTP_PORT / SMTP_USER / SMTP_PASS / SMTP_TO   邮箱(PORT 默认 465 走 SSL)
  RADAR_PUBLIC_URL     可选, 消息尾部的面板/日报访问链接

用法:
  python push_report.py --date 2026-09-12
  python push_report.py --date 2026-09-12 --dry-run
"""
import argparse
import base64
import datetime
import hashlib
import hmac
import json
import os
import smtplib
import time
import urllib.parse
import urllib.request
from email.header import Header
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import report
from normalize import DATA_DIR


def _cut(text, nbytes):
    raw = text.encode("utf-8")
    return raw[:nbytes].decode("utf-8", errors="ignore") if len(raw) > nbytes else text


def _link(e, text="原文"):
    """来源 markdown 链接; 无 URL 返回空串。"""
    src = e.get("source") or {}
    url = src.get("url")
    return "[%s](%s)" % (text, url) if url else ""


def brief_text(date, data):
    """政策日报 IM 卡片摘要(钉钉/企微不支持附件, 每条附原文链接便于核对)。"""
    new, active, covered = data["new"], data["active"], data["covered"]
    highs = [e for e in covered if e.get("impact_level") == "高影响" or e.get("action_type") == "收紧"]
    countries = sorted({e.get("country_cn") for e in new if e.get("country_cn")})
    lines = [
        "# 🛃 疫见全球 · 政策监测日报 %s" % date, "",
        "**新增政策变化 %d 条** · 收紧 %d · 放松/恢复 %d · 近期跟踪 %d · 待核实 %d" % (
            len(new), sum(1 for e in new if e.get("action_type") == "收紧"),
            sum(1 for e in new if e.get("action_type") in ("放松", "恢复")), len(active),
            sum(1 for e in covered if e.get("verification_status") == "unverified")), "",
        "**涉及国家**: %s" % ("、".join(countries[:8]) + ("等" if len(countries) > 8 else "") if countries else "—"),
        "", "**需要立即关注:**"]
    lines += ["- %s @ %s(%s)%s %s" % (
        e.get("title_cn") or e.get("title_en") or "（无标题）", e.get("country_cn"),
        e.get("impact_level") or "未研判",
        (" — " + str(e.get("impact_rationale", ""))[:60]) if e.get("impact_rationale") else "",
        _link(e)) for e in highs[:5]]
    if not highs: lines.append("- 本期无。")
    if new:
        lines += ["", "**今日新增一览**(点标题可核对原文):"]
        lines += ["- [%s](%s) — %s · %s%s" % (
            e.get("title_cn") or e.get("title_en") or "（无标题）",
            (e.get("source") or {}).get("url") or "", e.get("action_type") or "-",
            e.get("impact_type") or "未研判", "/" + e["impact_level"] if e.get("impact_level") else "")
            for e in new]
    lines += ["", "---",
              "全文见政策 Markdown / Word / Excel 报告(面板可下载)",
              "> AI 辅助生成 · 仅供情报参考"]
    return "\n".join(lines)


def _post_json(url, payload):
    req = urllib.request.Request(
        url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))


def send_wechat(text):
    # 企业微信 markdown 消息上限 4096 字节
    payload = {"msgtype": "markdown", "markdown": {"content": _cut(text, 3800)}}
    return _post_json(os.environ["WECHAT_WEBHOOK"], payload)


def send_dingtalk(text, title):
    url = os.environ["DINGTALK_WEBHOOK"]
    secret = os.environ.get("DINGTALK_SECRET")
    if secret:
        ts = str(round(time.time() * 1000))
        sign = base64.b64encode(hmac.new(
            secret.encode(), ("%s\n%s" % (ts, secret)).encode(), hashlib.sha256).digest())
        url += "&timestamp=%s&sign=%s" % (ts, urllib.parse.quote_plus(sign))
    payload = {"msgtype": "markdown", "markdown": {"title": title, "text": _cut(text, 18000)}}
    return _post_json(url, payload)


def send_mail(subject, text, docx_path):
    host = os.environ["SMTP_HOST"]
    port = int(os.environ.get("SMTP_PORT", "465"))
    user, pwd = os.environ["SMTP_USER"], os.environ["SMTP_PASS"]
    to = [x.strip() for x in os.environ["SMTP_TO"].split(",") if x.strip()]

    msg = MIMEMultipart()
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = user
    msg["To"] = ",".join(to)
    msg.attach(MIMEText(text.replace("\n", "<br>"), "html", "utf-8"))
    if docx_path and os.path.isfile(docx_path):
        part = MIMEApplication(open(docx_path, "rb").read())
        part.add_header("Content-Disposition", "attachment",
                        filename=Header(os.path.basename(docx_path), "utf-8").encode())
        msg.attach(part)

    if port == 465:
        srv = smtplib.SMTP_SSL(host, port, timeout=20)
    else:
        srv = smtplib.SMTP(host, port, timeout=20)
        srv.starttls()
    try:
        srv.login(user, pwd)
        srv.sendmail(user, to, msg.as_string())
    finally:
        srv.quit()
    return {"ok": True}


def main():
    ap = argparse.ArgumentParser(
        description="日报推送: 企业微信 / 钉钉 / 邮箱(渠道环境变量见模块 docstring 与 .env.example)")
    ap.add_argument("--date", default=datetime.date.today().isoformat())
    ap.add_argument("--days-back", type=int, default=14)
    ap.add_argument("--db-path", help="SQLite 路径覆盖")
    ap.add_argument("--message", help="发送自定义提醒文本(跳过日报汇总, 供 run_scan.sh 等调用)")
    ap.add_argument("--dry-run", action="store_true", help="只打印消息内容与目标渠道, 不发送")
    args = ap.parse_args()

    if args.message:
        text = args.message
        docx = None
    else:
        data = report.collect(args.date, args.days_back, args.db_path)
        if data is None:
            print("[提示] 政策库为空, 无可推送内容。")
            return 1
        text = brief_text(args.date, data)
        docx = os.path.join(DATA_DIR, "reports", "%s-policy-report.docx" % args.date)

    channels = []
    if os.environ.get("WECHAT_WEBHOOK"):
        channels.append(("企业微信", lambda: send_wechat(text)))
    if os.environ.get("DINGTALK_WEBHOOK"):
        channels.append(("钉钉", lambda: send_dingtalk(text, "疫见全球·政策监测日报 %s" % args.date)))
    if os.environ.get("SMTP_HOST"):
        channels.append(("邮箱(%s)" % os.environ.get("SMTP_TO", ""),
                         lambda: send_mail("疫见全球·政策监测日报 %s" % args.date, text, docx)))

    if args.dry_run or not channels:
        print(text)
        print("\n---")
        if not channels:
            print("[提示] 未配置任何推送渠道(WECHAT_WEBHOOK / DINGTALK_WEBHOOK / SMTP_*), "
                  "详见 .env.example")
            return 1
        print("[dry-run] 将发送到: %s(未实际发送)" % "、".join(c[0] for c in channels))
        return 0

    ok = 0
    for name, fn in channels:
        try:
            fn()
            print("[OK] %s 推送成功" % name)
            ok += 1
        except Exception as exc:
            print("[失败] %s: %s" % (name, exc))
    print("\n推送完成: %d/%d 个渠道成功。" % (ok, len(channels)))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
