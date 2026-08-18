#!/bin/sh
# 启动前把数据库带到最新状态，再交给 CMD。
#
# 迁移放在这里而不是单独的 compose 服务，是因为本项目强制单 worker：
# 只有一个进程会跑到这段，不存在多副本并发迁移的竞态。
set -e

echo "[entrypoint] alembic upgrade head"
alembic upgrade head

# init_db 自身是幂等的：权限种子按 code 去重，超管已存在时跳过。
# 未配置 INIT_SUPERUSER_* 且系统里一个超管都没有时，它会明确报错并终止——
# 这是刻意的，总好过起来一个没人能登录的服务。
echo "[entrypoint] bootstrap superuser & seeds"
python init_db.py

exec "$@"
