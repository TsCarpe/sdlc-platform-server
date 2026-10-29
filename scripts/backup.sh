#!/bin/bash
# MySQL 备份：mysqldump 快照到 backups/（保留最近 30 份）。建议 cron 每日执行。
# crontab 示例：0 2 * * * /path/to/sdlc-platform-server/scripts/backup.sh
# 依赖：mysqldump 客户端 + 环境变量 SDLC_DB_DSN（mysql://user:pass@host:3306/sdlc）。
#   --single-transaction：InnoDB 一致性快照不锁表（服务运行中可备）
#   --set-gtid-purged=OFF / --no-tablespaces：非 SUPER/PROCESS 权限账号可执行
# 恢复演练（每季至少一次才算数，EXEC-PLAN-K8s §3.6）：
#   mysql -h <host> -u <user> -p -e "CREATE DATABASE sdlc_restore"
#   gunzip -c backups/platform-<STAMP>.sql.gz | mysql -h <host> -u <user> -p sdlc_restore
set -e -o pipefail  # pipefail：mysqldump 失败不得被下游 gzip 吞掉
cd "$(dirname "$0")/.."
mkdir -p backups

if ! command -v mysqldump >/dev/null 2>&1; then
  echo "缺 mysqldump 客户端（macOS：brew install mysql-client && export PATH=\"\$(brew --prefix mysql-client)/bin:\$PATH\"；Linux：apt install mysql-client）" >&2
  exit 1
fi

if [ -z "$SDLC_DB_DSN" ]; then
  echo "需要 SDLC_DB_DSN（mysql://user:pass@host:3306/sdlc）" >&2
  exit 1
fi

# 从 DSN 拆连接参数（密码经 MYSQL_PWD 传，不进命令行 ps 可见面）
HOST=$(printf '%s' "$SDLC_DB_DSN" | sed -E 's|mysql://[^@]+@([^:/]+).*|\1|')
PORT=$(printf '%s' "$SDLC_DB_DSN" | sed -E 's|mysql://[^@]+@[^:/]+:([0-9]+).*|\1|')
USER=$(printf '%s' "$SDLC_DB_DSN" | sed -E 's|mysql://([^:]+):.*|\1|')
PASS=$(printf '%s' "$SDLC_DB_DSN" | sed -E 's|mysql://[^:]+:([^@]*)@.*|\1|')
DBNAME=$(printf '%s' "$SDLC_DB_DSN" | sed -E 's|.*/([^/?]+).*|\1|')
PORT=${PORT:-3306}
export MYSQL_PWD="${PASS//%40/@}"

STAMP=$(date +%Y%m%d-%H%M%S)
mysqldump --single-transaction --set-gtid-purged=OFF --no-tablespaces \
  -h "$HOST" -P "$PORT" -u "$USER" "$DBNAME" | gzip > "backups/platform-$STAMP.sql.gz"
# 保留最近 30 份快照，更早的清理
ls -t backups/platform-*.sql.gz 2>/dev/null | tail -n +31 | xargs rm -f 2>/dev/null || true
unset MYSQL_PWD
echo "已备份：backups/platform-$STAMP.sql.gz"
