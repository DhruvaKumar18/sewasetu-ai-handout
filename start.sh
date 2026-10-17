#!/bin/sh
set -e

echo "=== Sewa Setu Cloud Startup ==="

# Set default env vars if not provided
export SMS_GATEWAY_URL=${SMS_GATEWAY_URL:-http://127.0.0.1:8025}
export PORT=${PORT:-8000}

# 1. Initialize Database if needed (schema & seed)
echo "[startup] Checking and initializing database..."
python /app/app/init_db.py || echo "[startup] Warning: DB init script finished with warnings."

# 2. Start SMS Gateway in the background
echo "[startup] Starting SMS Gateway on port 8025..."
python /app/smsgw/app.py &

# 3. Run deemed approval once at startup to settle pending SLAs
echo "[startup] Running deemed approval check..."
python /app/app/scripts/deemed_approval.py || true

# 4. Start Gunicorn for main Sewa Setu portal
echo "[startup] Starting Gunicorn on port ${PORT}..."
exec gunicorn --bind 0.0.0.0:${PORT} --workers 4 --timeout 60 --chdir /app/app app:app
