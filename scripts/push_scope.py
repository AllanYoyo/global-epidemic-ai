#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""推送范围锁(scope lock): 把"本轮日报允许发到哪些渠道"从提示词变成落盘的事实。

为什么需要它(2026-10-20 钉钉误推事故复盘)
------------------------------------------
评审核闸门此前是"提示式"的: `run_scan.sh` 的 policy 档 prompt 写
"若本轮是用户在某个渠道发起的请求 → --review-channel; 若是定时任务 → --broadcast",
把"**谁发起了这次请求**"这个客观事实交给了 Agent 临场判断。三个结构性漏洞:

  ① `run_scan.sh` 是 cron 与手动补跑**共用**的入口 —— 手动补跑在 prompt 眼里
     就是"定时任务那条路", 于是 Agent 选了 --broadcast, 日报发到了钉钉;
  ② `run_scan.sh` 里**没有任何 REVIEW_CHANNEL 传递机制** —— 渠道信息只以自然
     语言出现在 prompt 里, 靠 Agent 照着拼命令; 拼错 = 等于没设闸门;
  ③ `push_report.py --broadcast` 是"自声明"的 —— 谁喊一声都能全量, 脚本无从
     判断这到底是不是定时任务。

现在: **谁发起, 由发起方显式声明, 并落盘成锁**。发起方(run_scan.sh / 人工)用
本模块写锁, `push_report.py` 读锁执行, 不再采信 Agent 的临场判断:

  锁文件 data/reports/.push-scope/<日期>.lock
      scope=review    channel=wechat  channel_label=企业微信  source=run_scan.sh:policy
      scope=broadcast source=run_scan.sh:policy

  scope=review     → 本轮只允许发该渠道; --broadcast 被拒绝(退出码 1)
  scope=broadcast  → 本轮允许全量(定时任务路径声明)
  没有锁           → --broadcast 被拒绝(全量只能由调度路径声明)

用法:
  python scripts/push_scope.py --date 2026-10-20 --review-channel wechat --source run_scan.sh:policy
  python scripts/push_scope.py --date 2026-10-20 --broadcast --source run_scan.sh:policy
  python scripts/push_scope.py --date 2026-10-20 --show
  python scripts/push_scope.py --date 2026-10-20 --clear
  python scripts/push_scope.py --print-label --review-channel wechat   # 只打印显示名
"""
import argparse
import os
import sys

from normalize import DATA_DIR, biz_now, biz_today

# 范围锁目录(与 .pushed / .review-pending 同级)
SCOPE_DIR = os.path.join(DATA_DIR, "reports", ".push-scope")

# 渠道键 → (环境变量名, 显示名)
REVIEW_CHANNEL_MAP = {
    "wechat": ("WECHAT_WEBHOOK", "企业微信"),
    "dingtalk": ("DINGTALK_WEBHOOK", "钉钉"),
    "mail": ("SMTP_HOST", "邮箱"),
}
# 显示名 → 环境变量名(用于"该渠道到底配没配"的校验)
REVIEW_CHANNEL_ENV = {label: env for env, label in REVIEW_CHANNEL_MAP.values()}
# 渠道别名: 中文与常见简写。认不出的**报错退出**(fail-closed), 绝不静默丢弃 ——
# 静默丢弃等价于"没设闸门" → 全量推送, 这正是 2026-10-19/20 事故的形态。
REVIEW_CHANNEL_ALIASES = {
    "wechat": "wechat", "wx": "wechat", "wecom": "wechat", "企业微信": "wechat",
    "dingtalk": "dingtalk", "ding": "dingtalk", "钉钉": "dingtalk",
    "mail": "mail", "email": "mail", "smtp": "mail", "邮箱": "mail", "邮件": "mail",
}


def scope_path(date):
    """锁文件路径: data/reports/.push-scope/<日期>.lock"""
    return os.path.join(SCOPE_DIR, "%s.lock" % date)


def read_scope(date):
    """读回本轮范围锁; 没有锁或读失败返回 None。"""
    path = scope_path(date)
    if not os.path.isfile(path):
        return None
    data = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                data[k.strip()] = v.strip()
    except OSError:
        return None
    return data or None


def write_scope(date, scope, channel=None, source="", note=""):
    """写入范围锁(原子替换)。scope 取 review / broadcast。"""
    os.makedirs(SCOPE_DIR, exist_ok=True)
    lines = ["scope=%s" % scope]
    if channel:
        lines.append("channel=%s" % channel)
        lines.append("channel_label=%s" % REVIEW_CHANNEL_MAP[channel][1])
    lines.append("source=%s" % (source or "-"))
    lines.append("declared_at=%s" % biz_now())
    if note:
        lines.append("note=%s" % note)
    path = scope_path(date)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    os.replace(tmp, path)
    return path


def clear_scope(date):
    """清掉范围锁; 返回是否真的删掉了。"""
    path = scope_path(date)
    if os.path.isfile(path):
        os.remove(path)
        return True
    return False


def resolve_channel(raw):
    """把用户写的渠道名解析成规范键(wechat/dingtalk/mail); 认不出返回 None。"""
    if not raw:
        return None
    return REVIEW_CHANNEL_ALIASES.get(str(raw).strip().lower())


def describe_scope(scope):
    """把锁内容翻译成一句人话(供 CLI 与诊断脚本打印)。"""
    if not scope:
        return "未声明"
    kind = scope.get("scope")
    if kind == "review":
        label = scope.get("channel_label") or scope.get("channel") or "?"
        return "只发 %s" % label
    if kind == "broadcast":
        return "全量(定时任务)"
    return kind or "未知"


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="推送范围锁: 声明/查看/清除某一轮日报允许推送的渠道范围")
    ap.add_argument("--date", default=biz_today(), help="业务日期(东八区), 默认今天")
    ap.add_argument("--review-channel", metavar="CHANNEL",
                    help="声明本轮只发该渠道(wechat/dingtalk/mail, 也认中文别名)")
    ap.add_argument("--broadcast", action="store_true",
                    help="声明本轮为全量推送(仅定时任务路径用)")
    ap.add_argument("--source", default="",
                    help="声明来源(写进锁里便于审计)。**全量声明必须显式给出**, "
                         "例如 run_scan.sh:policy:cron自动识别 / manual:运维补发; "
                         "留空会被拒绝 —— 10-20 事故就是一次'没写来源'的全量声明")
    ap.add_argument("--note", default="", help="备注(写进锁里)")
    ap.add_argument("--show", action="store_true", help="只打印当前范围锁, 不改写")
    ap.add_argument("--clear", action="store_true", help="清掉当前范围锁")
    ap.add_argument("--print-label", action="store_true",
                    help="只打印 --review-channel 的显示名(供 shell 取中文渠道名)")
    args = ap.parse_args(argv)

    if args.print_label:
        if not args.review_channel:
            print("[错误] --print-label 需要配合 --review-channel 使用")
            return 2
        key = resolve_channel(args.review_channel)
        if not key:
            print("[错误] 审阅渠道无法识别: %r" % args.review_channel)
            print("       合法取值: wechat | dingtalk | mail"
                  "(也认中文别名 企业微信 / 钉钉 / 邮箱)")
            return 1
        print(REVIEW_CHANNEL_MAP[key][1])
        return 0

    if args.show:
        scope = read_scope(args.date)
        if not scope:
            print("%s 未声明推送范围(无范围锁)" % args.date)
            return 0
        print("%s 推送范围: %s" % (args.date, describe_scope(scope)))
        for k in ("scope", "channel", "channel_label", "source", "declared_at", "note"):
            if scope.get(k):
                print("  %s: %s" % (k, scope[k]))
        return 0

    if args.clear:
        if clear_scope(args.date):
            print("[OK] 已清除 %s 的推送范围锁" % args.date)
            return 0
        print("[提示] %s 本来就没有范围锁" % args.date)
        return 0

    if args.review_channel and args.broadcast:
        print("[错误] --review-channel 与 --broadcast 互斥")
        return 1

    if args.review_channel:
        key = resolve_channel(args.review_channel)
        if not key:
            print("[错误] 审阅渠道无法识别: %r" % args.review_channel)
            print("       合法取值: wechat | dingtalk | mail"
                  "(也认中文别名 企业微信 / 钉钉 / 邮箱)")
            print("       为避免把日报发到所有渠道, 已中止; 修正后重试。")
            return 1
        path = write_scope(args.date, "review", channel=key, source=args.source,
                           note=args.note)
        print("[OK] 已写入推送范围锁: %s · 只发 %s(%s) · 来源 %s"
              % (args.date, REVIEW_CHANNEL_MAP[key][1], key, args.source))
        print("     %s" % path)
        return 0

    if args.broadcast:
        # 全量声明必须显式写来源: 10-20 事故的现场锁就是 source=manual(默认值),
        # 说明有人/有 Agent 直接敲了 push_scope.py --broadcast 就把本轮变成全量。
        # 来源留空 → 拒绝, 逼着声明方说清"这轮凭什么全量"。
        if not args.source.strip():
            print("[错误] 全量推送的范围声明必须显式给出 --source(写明依据)。")
            print("       · 定时任务: 由 scripts/run_scan.sh <模式> --broadcast 自动声明")
            print("       · 人工运维补发: --source manual:运维补发(人已确认扩散)")
            print("       不写来源的声明一律拒绝 —— 10-20 事故就是一条没写来源的全量声明。")
            return 1
        path = write_scope(args.date, "broadcast", source=args.source, note=args.note)
        print("[OK] 已写入推送范围锁: %s · 全量(定时任务) · 来源 %s"
              % (args.date, args.source))
        print("     %s" % path)
        return 0

    print("[错误] 请指定 --review-channel <渠道> 或 --broadcast(声明本轮推送范围)")
    return 2


if __name__ == "__main__":
    sys.exit(main())
