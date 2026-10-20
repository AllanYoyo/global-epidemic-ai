#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""运行代码版本指纹。

2026-10-20 钉钉误推事故排查时最费时间的不是"闸门逻辑对不对", 而是"**生产上
跑的到底是哪版代码**" —— 仓库里没有部署/更新脚本, VPS 上是一个 git clone 的工作
树, 没人 pull 的话跑的就是几个月前的旧逻辑, 现场无从判断。

现在推送内容尾部带一行 `代码 <短哈希>`, 事故复盘时一眼就能确认现场版本; 面板与
诊断脚本也用它核对"线上代码 == 仓库代码"。
"""
import os
import subprocess

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _git(args, timeout=5):
    """跑一条 git 命令; 失败返回 None(不抛异常 —— 版本指纹不是关键路径)。"""
    try:
        proc = subprocess.run(
            ["git", "-C", REPO] + args, capture_output=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    try:
        return proc.stdout.decode("utf-8", errors="ignore").strip()
    except Exception:
        return None


def git_commit(short=True):
    """当前 checkout 的 commit; 取不到返回 None。"""
    args = ["rev-parse", "--short", "HEAD"] if short else ["rev-parse", "HEAD"]
    out = _git(args)
    return out or None


def git_branch():
    """当前分支名; 取不到返回 None。"""
    return _git(["rev-parse", "--abbrev-ref", "HEAD"]) or None


def is_dirty():
    """已跟踪文件是否有未提交改动; 取不到返回 False。

    只看已跟踪文件: 未跟踪的运行时产物(data/geocache.json 之类)每次跑都在变,
    算进去会让版本指纹永远显示"+脏", 现场反而看不出真实状态。
    """
    out = _git(["status", "--porcelain", "--untracked-files=no"])
    return bool(out)


def untracked_files():
    """未跟踪文件列表(不阻塞部署, 只作提示)。"""
    out = _git(["status", "--porcelain"])
    if not out:
        return []
    return [ln[3:].strip() for ln in out.splitlines() if ln.startswith("??")]


def fingerprint():
    """版本指纹短串: 'b6506d0' / 'b6506d0+脏' / '未知(非 git 仓库)'。"""
    commit = git_commit()
    if not commit:
        return "未知(非 git 仓库)"
    if is_dirty():
        return "%s+脏" % commit
    return commit


def describe():
    """版本指纹详情(给诊断脚本用): 分支 + commit + 是否脏 + 仓库路径。"""
    return {
        "repo": REPO,
        "commit": git_commit(),
        "commit_full": git_commit(short=False),
        "branch": git_branch(),
        "dirty": is_dirty(),
        "untracked": untracked_files(),
        "fingerprint": fingerprint(),
    }
