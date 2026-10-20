#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""推送链路现场诊断: 回答"钉钉那条日报到底是怎么发出去的"。

2026-10-20 钉钉误推事故后, 排查最大的障碍不是闸门逻辑, 而是**现场证据**:
生产 VPS 上跑的是哪版代码? crontab 里的 run_scan.sh 有没有声明推送范围?
当天有没有待审标记/范围锁? cron 日志里那次推送走的哪个分支?

本脚本把这些问题一次性打印出来(全部只读, 不发任何消息), 在生产上直接跑:

    python3 scripts/diagnose_push.py               # 查今天
    python3 scripts/diagnose_push.py --date 2026-10-20
    python3 scripts/diagnose_push.py --log-tail 200

退出码: 0 = 未发现异常; 1 = 发现可疑项(见输出里的 [问题] / [警告])。
"""
import argparse
import os
import subprocess
import sys

import buildinfo
import push_scope
from normalize import DATA_DIR, biz_today

PUSH_FLAG_DIR = os.path.join(DATA_DIR, "reports", ".pushed")
PENDING_DIR = os.path.join(DATA_DIR, "reports", ".review-pending")
# 生产日志目录: 约定 /opt/global-epidemic-ai/logs, 可用环境变量覆盖
LOG_DIR = os.environ.get("EPIDEMIC_LOG_DIR") or "/opt/global-epidemic-ai/logs"
CRON_LOG = os.path.join(LOG_DIR, "cron.log")

problems = []


def _warn(msg):
    problems.append(msg)
    print("[警告] %s" % msg)


def _problem(msg):
    problems.append(msg)
    print("[问题] %s" % msg)


def _run(cmd, timeout=8):
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        return None, str(exc)
    if proc.returncode != 0:
        return None, proc.stderr.decode("utf-8", errors="ignore").strip()
    return proc.stdout.decode("utf-8", errors="ignore"), None


def section(title):
    print("\n=== %s ===" % title)


def check_code_version():
    section("1. 运行代码版本(生产上跑的是哪版?)")
    info = buildinfo.describe()
    print("  仓库路径: %s" % info["repo"])
    print("  当前分支: %s" % (info["branch"] or "?"))
    print("  当前提交: %s" % (info["commit"] or "?"))
    print("  工作区:   %s" % ("有未提交改动" if info["dirty"] else "干净"))
    branch = info["branch"] or "policy"
    out, err = _run(["git", "-C", info["repo"], "ls-remote", "origin", branch], timeout=10)
    if out:
        remote = out.split()[0][:7] if out.split() else ""
        if remote and info["commit"] and not info["commit"].startswith(remote):
            _problem("本地代码(%s)与 origin/%s(%s)不一致 —— 生产可能跑的是旧逻辑, "
                     "先执行 scripts/deploy_update.sh 更新" % (info["commit"], branch, remote))
        else:
            print("  远端:     origin/%s = %s(与本地一致)" % (branch, remote or "?"))
    else:
        print("  远端:     查不到(离线或无 origin 远端): %s" % (err or ""))


def check_scope_state(date):
    section("2. 当日推送状态(范围锁 / 待审 / 已推)")
    scope = push_scope.read_scope(date)
    if scope:
        print("  范围锁:   %s" % push_scope.describe_scope(scope))
        for k in ("source", "declared_at", "note"):
            if scope.get(k):
                print("            %s: %s" % (k, scope[k]))
        if scope.get("scope") == "review":
            print("            → 本轮只允许发 %s, --broadcast 会被拒绝"
                  % scope.get("channel_label", "?"))
    else:
        print("  范围锁:   无(未声明推送范围)")

    pending = os.path.join(PENDING_DIR, "%s.flag" % date)
    if os.path.isfile(pending):
        print("  待审标记: 有(%s)" % pending)
        with open(pending, encoding="utf-8") as f:
            for line in f:
                print("            %s" % line.rstrip())
    else:
        print("  待审标记: 无")

    pushed = os.path.join(PUSH_FLAG_DIR, "%s.flag" % date)
    if os.path.isfile(pushed):
        print("  已推标记: 有(%s)" % pushed)
        with open(pushed, encoding="utf-8") as f:
            for line in f:
                print("            %s" % line.rstrip())
    else:
        print("  已推标记: 无")


def check_channels():
    section("3. 推送渠道配置(哪些渠道会收到)")
    for env, label in push_scope.REVIEW_CHANNEL_MAP.values():
        val = os.environ.get(env)
        if val:
            print("  %-8s 已配置(%s=%s...)" % (label, env, str(val)[:24]))
        else:
            print("  %-8s 未配置(%s)" % (label, env))
    if not any(os.environ.get(env) for env, _ in push_scope.REVIEW_CHANNEL_MAP.values()):
        _warn("当前 shell 里没有读到任何推送渠道配置(正常: 配置在 ~/.hermes/.env, "
              "由 run_scan.sh 加载)")


def check_crontab():
    section("4. crontab 调度声明(定时任务有没有显式声明推送范围)")
    out, err = _run(["crontab", "-l"], timeout=8)
    if not out:
        print("  读不到 crontab: %s" % (err or "无输出"))
        return
    hits = [ln for ln in out.splitlines() if "run_scan.sh" in ln and not ln.strip().startswith("#")]
    if not hits:
        print("  crontab 里没有 run_scan.sh 调度")
        return
    for ln in hits:
        print("  %s" % ln.strip())
        if "--broadcast" in ln:
            print("            → 已显式声明全量推送 ✓")
        elif "--review-channel" in ln:
            print("            → 已显式声明审阅渠道 ✓")
        else:
            _warn("这条调度没有声明推送范围: 靠 cron 上下文自动识别为全量。"
                  "建议显式写成 `run_scan.sh <模式> --broadcast`")


def check_cron_log(log_path, tail):
    section("5. 调度日志尾部(那次推送走的哪个分支)")
    if not os.path.isfile(log_path):
        print("  找不到 %s(可用 --log 指定)" % log_path)
        return
    try:
        with open(log_path, encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
    except OSError as exc:
        print("  读取失败: %s" % exc)
        return
    picked = [ln.rstrip() for ln in lines[-tail:] if ln.strip()]
    for ln in picked:
        print("  %s" % ln)
    # 重点看有没有 scope= 标记(run_scan.sh 2026-10-20 起会打)
    if not any("scope=" in ln for ln in picked):
        _warn("日志里没有 scope= 标记 —— 说明这次跑的是 2026-10-20 之前的旧版 "
              "run_scan.sh(旧版没有推送范围声明, 默认全量推送)")
    # scope=none = 没能识别成定时任务上下文, 本轮不扩散(日报没推出去)
    for ln in picked:
        if "scope=none" in ln:
            _problem("日志里出现 scope=none: 本轮被判定为非定时任务上下文, "
                     "**没有推送**。若这行本该是 cron 调度, 说明 cron 识别失败 —— "
                     "请在 crontab 里显式加 --broadcast(见 config/crontab.example)")
            break


def main(argv=None):
    ap = argparse.ArgumentParser(description="推送链路现场诊断(只读)")
    ap.add_argument("--date", default=biz_today(), help="业务日期(东八区), 默认今天")
    ap.add_argument("--log", default=CRON_LOG, help="调度日志路径")
    ap.add_argument("--log-tail", type=int, default=60, help="日志尾部行数")
    args = ap.parse_args(argv)
    del problems[:]   # 同一进程内重复调用时, 结论只反映本次检查

    print("疫见全球 · 推送链路诊断  日期=%s" % args.date)
    check_code_version()
    check_scope_state(args.date)
    check_channels()
    check_crontab()
    check_cron_log(args.log, args.log_tail)

    section("结论")
    if problems:
        print("发现 %d 个需要处理的问题:" % len(problems))
        for p in problems:
            print("  · %s" % p)
        return 1
    print("未发现明显异常。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
