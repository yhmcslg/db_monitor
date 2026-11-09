#!/bin/bash
# Mac/Linux：提交并推送到 GitHub
# 用法:  ./commit.sh
# commit message 固定使用当前时间戳

set -e

cd "$(dirname "$0")"

REMOTE="https://github.com/yhmcslg/db_monitor.git"

# 确保 origin 存在且指向上面的地址
if git remote get-url origin >/dev/null 2>&1; then
    CURRENT=$(git remote get-url origin)
    if [ "$CURRENT" != "$REMOTE" ]; then
        echo "更新 origin: $CURRENT -> $REMOTE"
        git remote set-url origin "$REMOTE"
    fi
else
    git remote add origin "$REMOTE"
fi

MSG="update $(date '+%Y-%m-%d %H:%M:%S')"

# 当前分支
BRANCH=$(git rev-parse --abbrev-ref HEAD)

echo "分支: $BRANCH"
echo "消息: $MSG"
echo

git add -A

if git diff --cached --quiet; then
    echo "没有需要提交的改动，只执行 push"
else
    git commit -m "$MSG"
fi

git push -u origin "$BRANCH"

echo
echo "✔ 提交完成"
