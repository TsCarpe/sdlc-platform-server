#!/bin/bash
# SQLite 备份：拷贝库文件到 backups/（保留最近 30 份）。建议 cron 每日执行。
# crontab 示例：0 2 * * * /Users/hljy/IdeaProjects/sdlc-platform-server/scripts/backup.sh
set -e
cd "$(dirname "$0")/.."
DB=data/platform.db
mkdir -p backups

if [ ! -f "$DB" ]; then
  echo "库文件不存在：$DB" >&2
  exit 1
fi

STAMP=$(date +%Y%m%d-%H%M%S)
# .backup 命令保证一致性（即使服务运行中；WAL 模式下亦产出已 checkpoint 的一致快照）
sqlite3 "$DB" ".backup backups/platform-$STAMP.db"
# 保留最近 30 份快照，更早的清理
ls -t backups/platform-*.db 2>/dev/null | tail -n +31 | xargs rm -f 2>/dev/null || true
echo "已备份：backups/platform-$STAMP.db"
