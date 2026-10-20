#!/usr/bin/env bash
# 疫见全球 · 生产更新(在 VPS 上执行)
#
# 为什么要有这个脚本: 2026-10-20 钉钉误推事故排查时, 最大障碍是"生产上跑的到底是
# 哪版代码" —— 仓库里只有 README 的 git clone 步骤, 没有任何更新机制, 生产是
# /opt/global-epidemic-ai/repo 这个 clone 的工作树, 没人 pull 就一直跑旧逻辑。
# 闸门加了几轮加固, 生产没更新 = 加固根本没生效。
#
# 用法:
#   bash scripts/deploy_update.sh            # 更新当前分支
#   bash scripts/deploy_update.sh policy     # 指定分支
#   bash scripts/deploy_update.sh policy --no-deps   # 跳过依赖安装
set -u

BRANCH="${1:-policy}"
SKIP_DEPS=0
for a in "$@"; do
  [ "$a" = "--no-deps" ] && SKIP_DEPS=1
done

REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO" || exit 1

echo "[$(date '+%F %T')] deploy_update 开始: repo=$REPO branch=$BRANCH"

if ! command -v git >/dev/null 2>&1; then
  echo "[错误] 找不到 git"
  exit 1
fi

echo "--- 更新前状态 ---"
git rev-parse --short HEAD
git status --porcelain | head -20
if [ -n "$(git status --porcelain)" ]; then
  echo "[错误] 工作区有未提交改动, 先处理再更新(避免把现场改动覆盖掉):"
  git status --short
  exit 1
fi

echo "--- 拉取 $BRANCH ---"
git fetch origin "$BRANCH" || { echo "[错误] fetch 失败"; exit 1; }
git checkout "$BRANCH" || { echo "[错误] 切换分支失败"; exit 1; }
git pull --ff-only origin "$BRANCH" || {
  echo "[错误] 拉取失败(可能与远端分叉, 请人工处理)"
  exit 1
}
echo "--- 更新后状态 ---"
git log --oneline -3
git rev-parse --short HEAD

if [ "$SKIP_DEPS" -eq 0 ]; then
  echo "--- 安装依赖 ---"
  PIP_BIN="${PIP_BIN:-pip3}"
  command -v "$PIP_BIN" >/dev/null 2>&1 || PIP_BIN=pip
  "$PIP_BIN" install -r requirements.txt || echo "[警告] 依赖安装失败(脚本本身对缺依赖是优雅降级的)"
fi

echo "--- 链路自检 ---"
python3 scripts/diagnose_push.py || true

echo "[$(date '+%F %T')] deploy_update 完成"
