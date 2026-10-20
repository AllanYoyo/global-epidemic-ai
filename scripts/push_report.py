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
  python push_report.py --date 2026-09-12 --dry-run
  python push_report.py --date 2026-09-12 --review-channel wechat   # 阶段 1: 只发审阅渠道
  python push_report.py --date 2026-09-12 --review-approved        # 阶段 2: 发完剩余渠道
  python push_report.py --date 2026-09-12 --broadcast              # 定时任务: 显式全量
  python push_report.py --date 2026-09-12 --force                  # 当日已推送过, 强制重发

推送范围必须显式声明(2026-10-19 加固):
  日报推送必须给出 --review-channel(阶段 1) / --review-approved(阶段 2) /
  --broadcast(定时全量) 之一, 否则**不推送**并以退出码 2 提示。这是刻意的
  fail-closed: "忘了指定渠道"的后果应是"没发"(用户马上发现), 而不是"发到所有
  渠道"(要等别人投诉才发现)。

幂等: 日报摘要在同一天只发送一次, 成功后写 data/reports/.pushed/<日期>.flag;
重复调用自动跳过(防止 agent/定时任务重复推送)。--message 自定义消息不受限。

评审核闸门(2026-10-19 引入, 当晚加固)
------------------------------------
背景: 用户在微信/钉钉端提出测试或日扫请求时, 强调"反馈必须先在原渠道完成再扩散到其它渠道";
push_report.py 原版"配了哪个发哪个, 一发全发"做不到这一点 —— cron 触发或 Hermes 复述指令
都会把消息同时发给所有渠道。

启用: 传 `--review-channel <渠道>`(wechat / dingtalk / mail, 也认中文别名 企业微信/钉钉/邮箱
与简写 wx/wecom/email); 等价于环境变量 `REVIEW_CHANNEL=wechat`, 命令行参数优先。
两段式流程:
  阶段 1(审阅): push_report.py --date X --review-channel wechat → 只发审阅渠道, 写
                data/reports/.review-pending/<日期>.flag(首行记录审阅渠道),
                不写 .pushed/(此时其它渠道未推送, 二次调用默认跳过, 防误触)
  阶段 2(批准): 用户在审阅端确认 → push_report.py --date X --review-approved
                → 把审阅渠道之外的所有已配置渠道发完, 写 .pushed/, 删
                .review-pending/。下次调用跳过。
  阶段 2 的审阅渠道以 .review-pending 标记为准, --review-channel 只作兜底:
  2026-10-19 修复 —— 原实现只认环境变量, 阶段 2 忘设 REVIEW_CHANNEL 时会静默
  退化成"全量推送", 审阅渠道重复收到日报, 闸门形同虚设。

典型用法(Hermes 在微信会话里复述"先发微信审"的口径):
  Hermes 收到用户在微信端的请求 → push_report.py --date X --review-channel wechat
  → 收到用户在微信端的"ok / 发钉钉" → push_report.py --date X --review-approved

2026-10-19 晚加固(5 条漏法, 全部 fail-closed):
  ① 渠道名写错(如 weixin)曾被静默丢弃 → 等于"没设闸门" → 全量推送; 现在认不出
     就报错退出(中文别名/简写已入白名单)。
  ② --force 曾绕过待审保护 → 现在待审期间只发审阅渠道。
  ③ --message 曾完全绕过闸门; ④ 待审期间 --message 仍群发 —— 现在都只发审阅渠道。
  ⑤ "没指定渠道 = 全量发" 是最大的口子: 现在默认**不推送**(退出码 2), 全量必须
     显式 --broadcast(仅定时任务用)。

推送范围锁(2026-10-20 钉钉误推事故后引入, 决定性修复)
------------------------------------------------------
上面 5 条加固仍留着一个结构性漏洞: **"本轮是渠道会话还是定时任务"由 Agent 临场
判断**。而 `run_scan.sh` 是 cron 与手动补跑**共用**的入口 —— 手动补跑在 prompt
眼里就是"定时任务那条路", 于是 Agent 选了 --broadcast, 日报发到了钉钉
(2026-10-20 实测)。更糟的是 --broadcast 是"自声明"的: 谁喊一声都能全量, 脚本
无从判断这到底是不是定时任务。

现在推送范围**由发起方声明并落盘成锁**(scripts/push_scope.py):
  data/reports/.push-scope/<日期>.lock  →  scope=review|channel=... 或 scope=broadcast
  · scope=review    → 本轮只允许发该渠道; --broadcast 被拒绝(退出码 1)
  · scope=broadcast → 本轮允许全量(定时任务路径声明)
  · 没有锁          → --broadcast 被拒绝(全量只能由调度路径声明)
本脚本**读锁执行**, 不再采信 Agent 的临场判断; 阶段 1 自己也会写锁, 阶段 2 发完
清理锁。锁的写入方是 run_scan.sh(定时任务)或本脚本的 --review-channel(渠道会话)。
"""
import argparse
import base64
import datetime
import hashlib
import hmac
import json
import os
import smtplib
import subprocess
import time
import urllib.parse
import urllib.request
from email.header import Header
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import report
from buildinfo import fingerprint as code_fingerprint
from normalize import DATA_DIR, biz_now, biz_today
from push_scope import (HERMES_CHANNEL_MAP, HERMES_COMMAND, REVIEW_CHANNEL_ALIASES,
                        REVIEW_CHANNEL_ENV, REVIEW_CHANNEL_MAP,
                        channel_available, channel_key_for_label, channel_kind,
                        channel_label, channel_unavailable_hint, clear_scope,
                        describe_scope, hermes_broadcast_channels, read_scope,
                        write_scope)

PUSH_FLAG_DIR = os.path.join(DATA_DIR, "reports", ".pushed")
PENDING_DIR = os.path.join(DATA_DIR, "reports", ".review-pending")
# REVIEW_CHANNEL_MAP / REVIEW_CHANNEL_ENV / REVIEW_CHANNEL_ALIASES 的权威定义在
# push_scope.py(范围锁与闸门共用一份, 避免两处漂移), 这里保留同名引用供外部调用。


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
              "> AI 辅助生成 · 仅供情报参考 · 代码 %s" % code_fingerprint()]
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


def send_hermes(text, platform, docx_path=None):
    """借 `hermes send` 把消息投递到 Hermes 已配置的平台(个人微信 / 飞书)。

    为什么要这条通道(2026-10-20 生产现场查证):
      生产 `.env` 里**没有** WECHAT_WEBHOOK(企业微信群机器人), 用户说的"微信
      会话"是 Hermes 的个人微信通道(WEIXIN_*)。脚本自己 POST 发不进去 ——
      只有借 hermes send 才能把日报送进用户与 Hermes 说话的那个会话。
      `hermes send` 复用网关凭据, 不走 LLM、不需要 agent 循环;
      正文里写 `MEDIA:<path>` 可以把 Word 日报一并带过去。

    平台名用 hermes 的原生名(weixin / feishu), 裸平台名 = 发到 home channel。
    """
    body = text
    if docx_path and os.path.isfile(docx_path):
        body = "%s\nMEDIA:%s" % (text, docx_path)
    cmd = [HERMES_COMMAND, "send", "--to", platform, "--file", "-"]
    try:
        proc = subprocess.run(cmd, input=body.encode("utf-8"),
                              capture_output=True, timeout=90)
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError("hermes send 调用失败: %s" % exc)
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "ignore").strip()
        out = proc.stdout.decode("utf-8", "ignore").strip()
        raise RuntimeError("hermes send 退出码 %s: %s"
                           % (proc.returncode, err or out or "(无输出)"))
    return {"ok": True}


def _pending_path(date):
    return os.path.join(PENDING_DIR, "%s.flag" % date)


def _label_to_key(label):
    """显示名 → 渠道键(wechat/dingtalk/mail/weixin/feishu); 认不出返回 None。

    两类渠道(企业微信群机器人/钉钉/邮箱 与 经 hermes send 投递的微信/飞书)
    都要能反查, 否则写进范围锁的 channel 会是空的。
    """
    return channel_key_for_label(label)


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


def _build_channels(text, date, docx, extra_keys=None):
    """按配置组装可推送的渠道列表 [(显示名, sender_fn), ...]。

    两类渠道(2026-10-20 生产现场新增 Hermes 投递型):
    · webhook 型: 脚本自己 POST —— 企业微信群机器人 / 钉钉机器人 / SMTP 邮箱
    · Hermes 投递型: 借 `hermes send` 投到已配置平台 —— 个人微信 / 飞书。
      生产 .env 里没有 WECHAT_WEBHOOK, 用户的"微信会话"是 Hermes 的个人微信
      通道(WEIXIN_*), 脚本自己发不进去, 只有 hermes send 能把日报送进那个会话。

    显示名与 REVIEW_CHANNEL_MAP["review_label"] / HERMES_CHANNEL_MAP 中的
    显示名一一对应, 用于评审核闸门的渠道匹配; 因此不带动态后缀(如 SMTP_TO
    邮箱地址), 避免 "邮箱(x@y)" vs "邮箱" 这种字符串匹配不上。

    extra_keys: 额外要纳入本次集合的渠道键(如阶段 1 的审阅渠道)。
    定时全量集合 = 已配 webhook 渠道 + HERMES_PUSH_CHANNELS 指定的 Hermes 渠道
    (默认不把微信/飞书纳入全量, 免得日报突然刷屏; 要纳入用环境变量打开)。
    """
    channels = []
    if os.environ.get("WECHAT_WEBHOOK"):
        channels.append(("企业微信", lambda: send_wechat(text)))
    if os.environ.get("DINGTALK_WEBHOOK"):
        channels.append(("钉钉", lambda: send_dingtalk(text, "疫见全球·政策监测日报 %s" % date)))
    if os.environ.get("SMTP_HOST"):
        # 显示名固定为 "邮箱"(不带 SMTP_TO 邮箱地址), 与 REVIEW_CHANNEL_MAP["mail"] 一致
        channels.append(("邮箱", lambda: send_mail("疫见全球·政策监测日报 %s" % date, text, docx)))
    wanted = set(extra_keys or []) | set(hermes_broadcast_channels())
    for key in HERMES_CHANNEL_MAP:
        if key in wanted and channel_available(key):
            channels.append((HERMES_CHANNEL_MAP[key],
                             lambda k=key: send_hermes(text, k, docx)))
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
    ap.add_argument("--review-channel", metavar="CHANNEL",
                    help="评审核闸门·阶段 1 的审阅渠道(wechat|dingtalk|mail, 也认中文别名 "
                         "企业微信/钉钉/邮箱)。只发该渠道并写 .review-pending 待审标记; "
                         "用户确认后再用 --review-approved 发完其余渠道。"
                         "命令行参数优先于 REVIEW_CHANNEL 环境变量; 取值无法识别时报错退出")
    ap.add_argument("--broadcast", action="store_true",
                    help="显式全量推送(发给所有已配置渠道)。**仅定时任务用**: "
                         "渠道会话请求必须用 --review-channel, 否则违反"
                         "\"反馈先在原渠道完成再扩散到其它渠道\"")
    ap.add_argument("--review-approved", action="store_true",
                    help="评审核闸门: 审核通过, 发完审阅渠道之外的其它渠道, "
                         "并清理 .review-pending/<日期>.flag; 与 --review-channel 配对使用")
    args = ap.parse_args()

    flag_path = os.path.join(PUSH_FLAG_DIR, "%s.flag" % args.date)
    pending_path = _pending_path(args.date)

    # ---- 审阅渠道解析: 显式参数优先, 非法取值 fail-closed ----
    # 2026-10-19 修复(闸门漏法 1): 原实现把认不出的 REVIEW_CHANNEL 静默丢弃,
    # 等价于"没设闸门" → 全量推送。名字写错(最常见: 写中文"企业微信"或简写"wx")
    # 的代价变成"日报发到所有渠道", 正好踩中"先在原渠道完成再扩散"的红线。
    # 现在认不出的取值**报错退出** —— 宁可不发, 也不发错地方。
    raw_channel = (args.review_channel if args.review_channel is not None
                   else os.environ.get("REVIEW_CHANNEL", ""))
    raw_channel = str(raw_channel or "").strip()
    review_channel = None
    if raw_channel:
        review_channel = REVIEW_CHANNEL_ALIASES.get(raw_channel.lower())
        if not review_channel:
            print("[错误] 审阅渠道无法识别: %r" % raw_channel)
            print("       合法取值: wechat | dingtalk | mail"
                  "(也认中文别名 企业微信 / 钉钉 / 邮箱)")
            print("       为避免把日报发到所有渠道, 已中止; 修正后重试。")
            return 1

    if args.broadcast and review_channel:
        print("[错误] --broadcast 与 --review-channel 互斥: "
              "全量推送与\"只发审阅渠道\"不能同时指定")
        return 1

    # ---- 推送范围锁(2026-10-20 起): 范围由发起方声明, 本脚本只执行 ----
    # 锁: data/reports/.push-scope/<日期>.lock, 由 run_scan.sh(定时任务)或
    # 本脚本的 --review-channel(渠道会话)写入。目的是把"谁发起了这次请求"从
    # Agent 的临场判断变成落盘的事实 —— 10-20 钉钉误推正是"判断权"错位造成的。
    lock = read_scope(args.date)
    lock_scope = (lock or {}).get("scope") or ""
    lock_label = (lock or {}).get("channel_label") or ""
    lock_source = (lock or {}).get("source") or "-"
    if lock:
        print("[范围锁] %s 本轮推送范围: %s(声明来源: %s, 声明于 %s)"
              % (args.date, describe_scope(lock), lock_source,
                 (lock or {}).get("declared_at") or "-"))

    if args.broadcast:
        if lock_scope == "review":
            print("[错误] --broadcast 被推送范围锁拒绝: 本轮已声明为\"只发 %s\""
                  "(来源 %s)。渠道会话必须先在原渠道完成反馈, 用户确认后用 "
                  "--review-approved 发完其余渠道。" % (lock_label, lock_source))
            return 1
        if lock_scope != "broadcast":
            print("[错误] --broadcast 被拒绝: 本轮没有\"全量推送\"的范围声明。")
            print("       全量推送只能由调度路径声明, 二选一:")
            print("         · 定时任务: scripts/run_scan.sh <am|pm|policy> --broadcast")
            print("         · 人工声明: python scripts/push_scope.py --date %s --broadcast --source manual"
                  % args.date)
            print("       声明后再执行 push_report.py --date %s --broadcast。" % args.date)
            return 1
    elif lock_scope == "review" and review_channel and lock_label and not args.review_approved:
        # 锁里记着审阅渠道, 命令行却指定了别的渠道 → 拒绝(防"同一轮发到两个渠道")。
        # 阶段 2(--review-approved)不适用: 那时以待审标记为准, 参数只作兜底。
        want_label = channel_label(review_channel)
        if want_label != lock_label:
            print("[错误] 审阅渠道与推送范围锁不一致: 锁为 %s, 本次指定 %s。"
                  % (lock_label, want_label))
            print("       同一轮请求不得改投其它渠道; 确需更换请先执行: "
                  "python scripts/push_scope.py --date %s --clear" % args.date)
            return 1

    # 审阅渠道(阶段 1 要单独发的那个 / 阶段 2 要排除的那个):
    #   阶段 2(--review-approved): 待审标记里记录的渠道 > 范围锁里的渠道 > 显式指定
    #   有待审标记(未批准):        一律用标记里的渠道 —— 待审期间任何推送
    #                              (含 --force / --message)都不得扩散到其它渠道
    #   其余:                      范围锁里的渠道 > 显式指定的渠道
    pending_label = _pending_channel(args.date)
    env_label = channel_label(review_channel) if review_channel else ""
    if args.review_approved:
        review_label = pending_label or lock_label or env_label
    elif pending_label:
        review_label = pending_label
    else:
        review_label = lock_label or env_label
    if args.broadcast and pending_label and not args.review_approved:
        print("[提示] 该日期存在待审标记, --broadcast 被闸门挡下: 只发审阅渠道 %s; "
              "用户确认后用 --review-approved 发完其余渠道" % pending_label)

    # --message 走自定义文本, 不走日报汇总(通常是 cron fallback 推"待执行提醒",
    # 没有日报可审)。但**待审期间**或**显式指定了审阅渠道**时, 自定义文本同样
    # 不得扩散到其它渠道(2026-10-19 修复闸门漏法 3/4: 原实现 --message 完全
    # 绕过闸门, 待审期间钉钉群照样收到提醒)。
    if args.message:
        text = args.message
        docx = None
        # 审阅渠道(Hermes 型)要显式纳入本次集合, 否则只发钉钉就发不到微信
        review_key = channel_key_for_label(review_label) if review_label else None
        channels = _build_channels(text, args.date, docx,
                                   extra_keys=[review_key] if review_key else None)
        if review_label:
            channels = [c for c in channels if c[0] == review_label]
        if args.dry_run or not channels:
            print(text)
            print("\n---")
            if not channels:
                if review_label:
                    print("[失败] 审阅渠道 %s 不可用: %s"
                          % (review_label, channel_unavailable_hint(review_label)))
                else:
                    print("[提示] 未配置任何推送渠道(WECHAT_WEBHOOK / DINGTALK_WEBHOOK / SMTP_*), "
                      "详见 .env.example")
                return 1
            print("[dry-run] 将发送到: %s(未实际发送)" % "、".join(c[0] for c in channels))
            return 0
        # 自定义文本也要把范围落成锁: 否则本轮之后的一次 --broadcast 仍能自我放行
        # (锁是"本轮范围"的唯一事实来源)。dry-run 不写(只读预演不留状态)。
        if review_label and lock_scope != "review":
            write_scope(args.date, "review", channel=_label_to_key(review_label),
                        source="push_report.py:message",
                        note="自定义消息的审阅渠道")
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
    # 审阅渠道(Hermes 型)要显式纳入本次集合, 否则只发钉钉就发不到微信
    review_key = channel_key_for_label(review_label) if review_label else None
    channels = _build_channels(text, args.date, docx,
                               extra_keys=[review_key] if review_key else None)

    # 决定本次实际推送的渠道集合
    # 审阅渠道(review_label)已在 main() 开头解析完毕, 优先级:
    #   阶段 2(--review-approved): 待审标记里记录的渠道 > 显式指定的渠道
    #   有待审标记(未批准):        一律用标记里的渠道(待审期间不得扩散)
    #   其余:                      显式指定的渠道(--review-channel / REVIEW_CHANNEL)
    if review_label and not args.review_approved:
        # 审阅渠道必须真的可用(webhook 型看环境变量, Hermes 型看 hermes CLI),
        # 否则"只发审阅渠道"= 一条都不发, 要报错而不是静默
        review_key = channel_key_for_label(review_label)
        if not review_key or not channel_available(review_key):
            print("[失败] 审阅渠道 %s 不可用: %s"
                  % (review_label, channel_unavailable_hint(review_label)))
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
    elif args.broadcast:
        # 显式全量(定时任务)
        target = channels
        stage_label = "全量(--broadcast)"
    else:
        # 2026-10-19 修复(闸门漏法 5): **默认不扩散**。
        # 原实现是"没指定审阅渠道 = 全量发", 于是任何一次"忘了指定渠道"的调用
        # (Agent 照抄命令 / 参数拼错 / 换了会话)都会把日报发到所有渠道。
        # 现在扩散必须**显式声明**(--broadcast), 忘了就变成"不发" ——
        # "没消息"用户马上会发现, "发到所有渠道"要等别人投诉才发现。
        if args.review_approved:
            # 2026-10-20 修复: 阶段 2 读不到审阅渠道时, 原实现"警告一下照常全量发"
            # —— 这是又一个"忘了 → 发到所有渠道"的口子(没有待审记录却喊批准)。
            # 现在直接拒绝: 没有待审记录就没有"批准"的对象。
            print("[错误] 阶段 2 无审阅渠道可读(无 .review-pending 待审标记, 也没有"
                  "推送范围锁), 已中止。")
            print("       阶段 2 必须紧跟在阶段 1(--review-channel <渠道>)之后执行; "
                  "若确实要全量, 请显式声明: ")
            print("         python scripts/push_scope.py --date %s --broadcast --source manual" % args.date)
            print("         python scripts/push_report.py --date %s --broadcast" % args.date)
            return 1
        else:
            print("[错误] 未指定推送范围, 已中止(默认不扩散)。请明确选择其一:")
            print("       · 渠道会话(先在原渠道完成, 确认后再扩散):")
            print("         python scripts/push_report.py --date %s --review-channel wechat" % args.date)
            print("         用户确认后: python scripts/push_report.py --date %s --review-approved" % args.date)
            print("       · 定时任务(全量, 需先由调度路径声明范围):")
            print("         scripts/run_scan.sh <am|pm|policy> --broadcast")
            return 2

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
            if lock_scope == "review":
                clear_scope(args.date)
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
            print("[dry-run] 评审核闸门·阶段 1 预览: 只发 %s(审阅渠道=%s); "
                  "审阅通过后用 --review-approved 发完剩余渠道"
                  % (review_label, raw_channel or review_label))
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
            # 同时把范围锁落成 review —— 这样本轮之后任何"忘了指定渠道"的调用
            # (尤其是 --broadcast)都会被锁挡下, 而不是退化成全量推送。
            # 2026-10-20 钉钉误推事故的直接教训: 闸门不能只靠"记得带参数"。
            if lock_scope != "review":
                write_scope(args.date, "review", channel=_label_to_key(review_label),
                            source="push_report.py:stage1",
                            note="阶段 1 审阅渠道")
        else:
            # 阶段 2 或全量模式: 写终态。
            # 带上"范围是怎么来的"(锁的声明来源), 下次事故复盘时能一眼看出
            # 是定时任务路径广播的, 还是有人手动声明的 —— 10-20 就是靠这个定的性。
            os.makedirs(PUSH_FLAG_DIR, exist_ok=True)
            with open(flag_path, "w", encoding="utf-8") as f:
                f.write("%s 已推送: %s · 范围=%s · 声明来源=%s\n" %
                        (biz_now(), "、".join(c[0] for c in target),
                         describe_scope(lock) if lock else "未声明(直接指定渠道)",
                         lock_source))
            _clear_pending(args.date)
            # 本轮已扩散完毕, 范围锁失效(否则同日再有人喊 --broadcast 会被误挡,
            # 而这一轮的"扩散"已经由用户批准过了)
            if lock_scope == "review":
                clear_scope(args.date)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
