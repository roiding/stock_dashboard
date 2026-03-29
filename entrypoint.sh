#!/bin/sh
set -e

echo "[entrypoint] Waiting for MySQL..."
for i in $(seq 1 30); do
  python -c "
from config import Config
from sqlalchemy import create_engine
e = create_engine(Config.SQLALCHEMY_DATABASE_URI)
e.connect().close()
" 2>/dev/null && break
  echo "  retry $i/30..."
  sleep 2
done

echo "[entrypoint] Running init_db..."
python init_db.py

# 安装策略脚本的额外依赖 (用户通过 UI 维护)
if [ -f /data/requirements_extra.txt ]; then
  echo "[entrypoint] Installing extra requirements..."
  pip install -r /data/requirements_extra.txt || echo "  (some packages failed)"
fi

echo "[entrypoint] Starting dashboard..."
exec python app.py
