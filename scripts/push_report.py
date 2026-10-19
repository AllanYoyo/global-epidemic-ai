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
  REVIEW_CHANNEL       (可选) 评审核闸门的目标渠道, 取值 wechat|dingtalk|mail;
                       设为非空时启用"先审后发"两段式: 首跑只发该渠道并写
                       .review-pending.<日期>.flag, 待人工在审阅端确认后用
                       --review-approved 才把其余渠道发完, 详见下文"评审核闸门"。

用法:
  python push_report.py --date 2026-09-12
  python push_report.py --date 2026-09-12 --dry-run
  python push_report.py --date 2026-09-12 --force   # 当日已推送过, 强制重发
  python push_report.py --date 2026-09-12 --review-approved   # 审核通过, 发完剩余渠道

幂等: 日报摘要在同一天只发送一次, 成功后写 data/reports/.pushed/<日期>.flag;
重复调用自动跳过(防止 agent/定时任务重复推送)。--message 自定义消息不受限。

评审核闸门(2026-10-19 引入)
----------------------------
背景: 用户在微信/钉钉端提出测试或日扫请求时, 强调"反馈必须先在原渠道完成再扩散到其它渠道";
push_report.py 原版"配了哪个发哪个, 一发全发"做不到这一点 —— cron 触发或 Hermes 复述指令
都会把消息同时发给所有渠道。

启用: 设环境变量 `REVIEW_CHANNEL=wechat`(取值 wechat / dingtalk / mail)。
两段式流程:
  阶段 1(审阅): push_report.py --date X  → 只发审阅渠道(wechat), 写
                data/reports/.review-pending/<日期>.flag(首行记录审阅渠道),
                不写 .pushed/(此时其它渠道未推送, 二次调用默认跳过, 防误触)
  阶段 2(批准): 用户在审阅端确认 → push_report.py --date X --review-approved
                → 把审阅渠道之外的所有已配置渠道发完, 写 .pushed/, 删
                .review-pending/。下次调用跳过。
  阶段 2 的审阅渠道以 .review-pending 标记为准, REVIEW_CHANNEL 只作兜底:
  2026-10-19 修复 —— 原实现只认环境变量, 阶段 2 忘设 REVIEW_CHANNEL 时会静默
  退化成"全量推送", 审阅渠道重复收到日报, 闸门形同虚设。

典型用法(Hermes 在微信会话里复述"先发微信审"的口径):
  HERMES_RUN_CMD 不配置时, run_scan.sh cron 会退化推"待执行提醒"; 在 Hermes
  会话中显式调用 push_report.py 时, 由会话侧把 REVIEW_CHANNEL=wechat 设上,
  Hermes 收到用户在微信端的"ok / 发钉钉"再 --review-approved。

不启用(默认, 向后兼容): 不设 REVIEW_CHANNEL, 行为与 2026-10-19 之前完全一致:
所有配置渠道一次性发出, 写 .pushed/。
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
from normalize import DATA_DIR, biz_now, biz_today

PUSH_FLAG_DIR = os.path.join(DATA_DIR, "reports", ".pushed")
PENDING_DIR = os.path.join(DATA_DIR, "reports", ".review-pending")
# REVIEW_CHANNEL 取值 → (环境变量名, 显示名)
REVIEW_CHANNEL_MAP = {
    "wechat": ("WECHAT_WEBHOOK", "企业微信"),
    "dingtalk": ("DINGTALK_WEBHOOK", "钉钉"),
    "mail": ("SMTP_HOST", "邮箱"),
}


def _cut(text, nbytes):
    raw = text.encode("utf-8")
    return raw[:nbytes].decode("utf-8", errors="ignore") if len(raw) > nbytes else text


def _cut_sentence(text, limit=60):
    """按句子截断, 避免依据被拦腰切断。"""
    text = str(text or "").strip()
    if len(text) <= limit:
        return text
    head = text[:limit]
    pos = head.find("。")
    return head[:pos + 1] if pos > 10 else head.rstrip("，、；") + "…"


def _title(e):
    # 显示层兜底链: title_cn → title_en → summary_cn → "（无标题）"
    # 校验只要求三选一, 显示层必须把三种都试一遍, 否则会出现"入库合法但
    # 推送时显示无标题"的鬼影记录(report.py / report_docx.py 同样的兜底链)。
    return e.get("title_cn") or e.get("title_en") or e.get("summary_cn") or "（无标题）"


def brief_text(date, data):
    """政策日报 IM 卡片摘要: 重点关注(中影响/收紧) + 其他新增, 每条附原文链接。"""
    new, active = data["new"], data["active"]
    countries = sorted({e.get("country_cn") for e in new if e.get("country_cn")})
    act = {"收紧": 0, "调整": 1, "放松": 2, "恢复": 3}
    lines = [
        "# 🛃 疫见全球 · 政策监测日报 %s" % date, "",
        "**新增 %d 条** · 收紧 %d · 调整 %d · 放松/恢复 %d · 近期跟踪 %d · 待核实 %d" % (
            len(new),
            sum(1 for e in new if e.get("action_type") == "收紧"),
            sum(1 for e in new if e.get("action_type") == "调整"),
            sum(1 for e in new if e.get("action_type") in ("放松", "恢复")), len(active),
            sum(1 for e in data["covered"] if e.get("verification_status") == "unverified")), "",
        "**涉及国家**: %s" % ("、".join(countries[:8]) + ("等" if len(countries) > 8 else "") if countries else "—"),
    ]
    # 重点关注口径与 report.py 保持一致(2026-10-19 修正): 原实现只看"中影响 or 收紧",
    # 把合同里最重的一档"高影响"漏进"其他新增" —— 用户第一眼看的 IM 卡片反而把
    # 最该关注的政策藏起来了。排序: 高影响 > 中影响 > 收紧。
    lv_rank = {"高影响": 0, "中影响": 1}
    focus = [e for e in new if e.get("impact_level") in lv_rank or e.get("action_type") == "收紧"]
    focus_ids = {x["event_id"] for x in focus}
    rest = [e for e in new if e.get("event_id") not in focus_ids]
    focus.sort(key=lambda e: (lv_rank.get(e.get("impact_level"), 2),
                              act.get(e.get("action_type"), 9)))
    rest.sort(key=lambda e: (act.get(e.get("action_type"), 9),
                              {"高影响": 0, "中影响": 1, "低影响": 2}.get(e.get("impact_level"), 3)))
    if focus:
        lines += ["", "**重点关注** ⚠️"]
        for e in focus:
            line = "- [%s](%s) %s@%s · %s/%s" % (
                _title(e), (e.get("source") or {}).get("url") or "",
                e.get("action_type") or "-", e.get("country_cn") or "-",
                e.get("impact_type") or "未研判", e.get("impact_level") or "未研判")
            if e.get("impact_level") in ("高影响", "中影响") and e.get("impact_rationale"):
                line += " — " + _cut_sentence(e["impact_rationale"])
            lines.append(line)
    if rest:
        lines += ["", "**其他新增**:"]
        for e in rest:
            lines.append("- [%s](%s) %s@%s · %s/%s" % (
                _title(e), (e.get("source") or {}).get("url") or "",
                e.get("action_type") or "-", e.get("country_cn") or "-",
                e.get("impact_type") or "未研判", e.get("impact_level") or "未研判"))
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


def _pending_path(date):
    return os.path.join(PENDING_DIR, "%s.flag" % date)


def _is_pending(date):
    return os.path.isfile(_pending_path(date))


def _mark_pending(date, channel_name):
    os.makedirs(PENDING_DIR, exist_ok=True)
    with open(_pending_path(date), "w", encoding="utf-8") as f:
        # 第一行是机器可读的审阅渠道, 供阶段 2 读回(见 _pending_channel);
        # 其余是给人看的说明。旧版本写的 flag 没有这一行, 读回返回 None, 自动回退
        # 到 REVIEW_CHANNEL 环境变量, 保持向后兼容。
        f.write("review_channel=%s\n" % channel_name)
        f.write("%s 等待审核: 已发至 %s, 待 --review-approved 发完其它渠道\n" %
                (biz_now(), channel_name))


def _pending_channel(date):
    """从待审标记里读回"阶段 1 实际发往哪个渠道"; 读不到返回 None。

    2026-10-19 引入: 阶段 2 必须知道审阅渠道是谁, 才能保证"审阅渠道不重复收报"。
    只认环境变量 REVIEW_CHANNEL 是不够的 —— 阶段 2 换个会话/换台机器执行时
    很容易忘带这个变量, 一旦忘带, 闸门会静默退化成"全量推送", 审阅渠道重复收到
    日报, 直接违反"反馈先在原渠道完成再扩散"的要求。
    """
    path = _pending_path(date)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                if line.startswith("review_channel="):
                    label = line.split("=", 1)[1].strip()
                    return label or None
    except OSError:
        return None
    return None


def _clear_pending(date):
    p = _pending_path(date)
    if os.path.isfile(p):
        os.remove(p)


def _build_channels(text, date, docx):
    """按环境变量组装可推送的渠道列表 [(显示名, sender_fn), ...]。

    显示名与 REVIEW_CHANNEL_MAP 中的 review_label 一一对应, 用于评审核闸门的
    渠道匹配; 因此不带动态后缀(如 SMTP_TO 邮箱地址), 避免 "邮箱(x@y)" vs "邮箱"
    这种字符串匹配不上。
    """
    channels = []
    if os.environ.get("WECHAT_WEBHOOK"):
        channels.append(("企业微信", lambda: send_wechat(text)))
    if os.environ.get("DINGTALK_WEBHOOK"):
        channels.append(("钉钉", lambda: send_dingtalk(text, "疫见全球·政策监测日报 %s" % date)))
    if os.environ.get("SMTP_HOST"):
        # 显示名固定为 "邮箱"(不带 SMTP_TO 邮箱地址), 与 REVIEW_CHANNEL_MAP["mail"] 一致
        channels.append(("邮箱", lambda: send_mail("疫见全球·政策监测日报 %s" % date, text, docx)))
    return channels


def main():
    ap = argparse.ArgumentParser(
        description="日报推送: 企业微信 / 钉钉 / 邮箱(渠道环境变量见模块 docstring 与 .env.example)")
    ap.add_argument("--date", default=biz_today())
    ap.add_argument("--days-back", type=int, default=14)
    ap.add_argument("--db-path", help="SQLite 路径覆盖")
    ap.add_argument("--message", help="发送自定义提醒文本(跳过日报汇总, 供 run_scan.sh 等调用)")
    ap.add_argument("--dry-run", action="store_true", help="只打印消息内容与目标渠道, 不发送")
    ap.add_argument("--force", action="store_true", help="当日日报已推送过也强制重发")
    ap.add_argument("--review-approved", action="store_true",
                    help="评审核闸门: 审核通过, 发完审阅渠道之外的其它渠道, "
                         "并清理 .review-pending/<日期>.flag; 与 REVIEW_CHANNEL=... 配对使用")
    args = ap.parse_args()

    flag_path = os.path.join(PUSH_FLAG_DIR, "%s.flag" % args.date)
    pending_path = _pending_path(args.date)
    review_channel = os.environ.get("REVIEW_CHANNEL", "").strip().lower()
    review_channel = review_channel if review_channel in REVIEW_CHANNEL_MAP else ""

    # --message 走自定义文本, 不走日报汇总; 也不参与评审核闸门(通常是 cron fallback
    # 推"待执行提醒", 没有日报可审)。允许照常发到所有渠道(不写 .pushed, 不影响日报幂等)。
    if args.message:
        text = args.message
        docx = None
        channels = _build_channels(text, args.date, docx)
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
        print("\n自定义消息推送完成: %d/%d 个渠道成功。" % (ok, len(channels)))
        return 0 if ok else 1

    # 走日报汇总分支
    if not args.dry_run and not args.force:
        # 终态标记: 全部渠道已完成, 跳过
        if os.path.isfile(flag_path):
            # [WARN] 而非 [跳过]: 走的是正常幂等分支, 但外部监控只盯 exit code 会看不出
            # "今天没推", 与"推送成功"无法区分。WARN 前缀便于告警脚本 grep。
            print("[WARN] %s 日报已推送过(标记: %s), 本次未推送; 需重发请加 --force"
                  % (args.date, flag_path))
            return 0
        # 审核阶段标记: 只发过审阅渠道, 等 --review-approved
        if os.path.isfile(pending_path) and not args.review_approved:
            with open(pending_path, encoding="utf-8") as f:
                pending_note = f.read().strip()
            print("[WARN] %s 日报处于待审状态(%s); 本次未推送。\n"
                  "  审核通过请加 --review-approved; 重审请加 --force(会重发审阅渠道)"
                  % (args.date, pending_note))
            return 0

    try:
        data = report.collect(args.date, args.days_back, args.db_path)
    except ValueError as exc:
        print("[错误] %s" % exc)
        return 2
    if data is None:
        print("[提示] 政策库为空, 无可推送内容。")
        return 1
    text = brief_text(args.date, data)
    docx = os.path.join(DATA_DIR, "reports", "%s-policy-report.docx" % args.date)
    channels = _build_channels(text, args.date, docx)

    # 决定本次实际推送的渠道集合
    # 审阅渠道(要排除/要单独发的那个渠道)按下面优先级确定:
    #   阶段 2(--review-approved): 待审标记里记录的渠道 > REVIEW_CHANNEL 环境变量
    #   阶段 1(未批准):            REVIEW_CHANNEL 环境变量
    # 2026-10-19 修正: 原实现阶段 2 只认环境变量, 忘设 REVIEW_CHANNEL 时会静默
    # 退化成"全量推送" —— 审阅渠道重复收到日报, 闸门形同虚设(实测复现)。
    pending_label = _pending_channel(args.date)
    env_label = REVIEW_CHANNEL_MAP[review_channel][1] if review_channel else ""
    if args.review_approved:
        review_label = pending_label or env_label
    else:
        review_label = env_label

    if review_channel and not args.review_approved:
        review_env = REVIEW_CHANNEL_MAP[review_channel][0]
        if not os.environ.get(review_env):
            print("[失败] REVIEW_CHANNEL=%s 但 %s 未配置, 评审核闸门无法启用"
                  % (review_channel, review_env))
            return 1

    if review_label:
        if args.review_approved:
            # 阶段 2: 发审阅渠道之外的所有已配渠道
            target = [c for c in channels if c[0] != review_label]
            stage_label = "审核通过(发完剩余渠道, 已排除审阅渠道 %s)" % review_label
        else:
            # 阶段 1: 只发审阅渠道
            target = [c for c in channels if c[0] == review_label]
            if not target:
                print("[失败] 审阅渠道 %s 未配置或环境变量名错配" % review_label)
                return 1
            stage_label = "审核(仅发 %s)" % review_label
    else:
        # 普通模式: 全部渠道(向后兼容)
        if args.review_approved:
            # 阶段 2 却读不到审阅渠道(既无待审标记, 也无 REVIEW_CHANNEL):
            # 只能按全量发, 但必须说明白, 别让人以为闸门生效了。
            print("[警告] 未读到审阅渠道(无 .review-pending 标记且未设 REVIEW_CHANNEL), 按全量推送")
        target = channels
        stage_label = "全量"

    if not target:
        if args.dry_run:
            # 2026-10-19 修正: 原来这里一律打"评审核闸门开启但剩余渠道无配置可发",
            # 但"一个推送渠道都没配"时闸门根本没开, 这条提示会把人带偏
            # (以为闸门挡住了, 实际是没配渠道)。
            if review_label or args.review_approved:
                print("[dry-run] 评审核闸门开启但剩余渠道无配置可发, --review-approved 视为 no-op")
            else:
                print("[dry-run] 未配置任何推送渠道(WECHAT_WEBHOOK / DINGTALK_WEBHOOK / SMTP_*), "
                      "详见 .env.example; 无渠道可发送")
            return 0
        # 非 dry-run 阶段 2 命中: 没有任何剩余渠道要发, 视为已结束, 写终态标记
        if review_label and args.review_approved:
            os.makedirs(PUSH_FLAG_DIR, exist_ok=True)
            with open(flag_path, "w", encoding="utf-8") as f:
                f.write("%s 已推送(仅审阅渠道 %s, 无剩余渠道)\n" %
                        (biz_now(), review_label))
            _clear_pending(args.date)
            print("[OK] 审阅渠道外无可发渠道, 已写终态标记并清理审核待办")
            return 0
        if not channels:
            print("[失败] 未配置任何推送渠道(WECHAT_WEBHOOK / DINGTALK_WEBHOOK / SMTP_*), "
                  "详见 .env.example")
            return 1
        print("[失败] 当前模式无目标渠道")
        return 1

    if args.dry_run:
        print(text)
        print("\n---")
        if review_label and not args.review_approved:
            print("[dry-run] 评审核闸门·阶段 1 预览: 只发 %s(REVIEW_CHANNEL=%s); "
                  "审阅通过后用 --review-approved 发完剩余渠道"
                  % (review_label, review_channel))
        elif review_label and args.review_approved:
            remaining = [c[0] for c in channels if c[0] != review_label]
            if remaining:
                print("[dry-run] 评审核闸门·阶段 2 预览: 审核通过(已排除审阅渠道 %s), 将发送到: %s"
                      % (review_label, "、".join(remaining)))
            else:
                print("[dry-run] 评审核闸门·阶段 2 预览: 审核通过, 剩余渠道无配置可发")
        else:
            print("[dry-run] 模式: %s; 将发送到: %s(未实际发送)" %
                  (stage_label, "、".join(c[0] for c in target)))
        return 0

    ok = 0
    for name, fn in target:
        try:
            fn()
            print("[OK] %s 推送成功" % name)
            ok += 1
        except Exception as exc:
            print("[失败] %s: %s" % (name, exc))
    print("\n推送完成(%s): %d/%d 个渠道成功。" % (stage_label, ok, len(target)))

    # 标记终态: 阶段 1 写 .review-pending, 阶段 2 写 .pushed 并清理待办
    if not args.message and ok == len(target) and target:
        if review_label and not args.review_approved:
            # 阶段 1 成功: 写待审标记(带机器可读的 review_channel 行, 供阶段 2 读回)
            _mark_pending(args.date, review_label)
        else:
            # 阶段 2 或全量模式: 写终态
            os.makedirs(PUSH_FLAG_DIR, exist_ok=True)
            with open(flag_path, "w", encoding="utf-8") as f:
                f.write("%s 已推送: %s\n" %
                        (biz_now(), "、".join(c[0] for c in target)))
            _clear_pending(args.date)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
