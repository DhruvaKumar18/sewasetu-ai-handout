"""
Sewa Setu - Local Runner Script
Starts the Sewa Setu Flask application with all local configurations.
"""
import os
import sys

# Set all local environment variables explicitly
local_env = {
    "DB_HOST": "127.0.0.1",
    "DB_PORT": "5433",
    "DB_USER": "sewasetu",
    "DB_PASSWORD": "help",
    "DB_NAME": "sewasetu",
    "SMS_GATEWAY_URL": "http://127.0.0.1:8025",
    "UPLOAD_DIR": os.path.join(os.path.dirname(os.path.abspath(__file__)), "uploads"),
    "PUBLIC_BASE_URL": "http://localhost:8000",
    "ADMIN_USERNAME": "admin",
    "ADMIN_PASSWORD": os.environ.get("ADMIN_PASSWORD", "admin123"),
    "ADMIN_BEARER_TOKEN": os.environ.get("ADMIN_BEARER_TOKEN", "sewasetu-admin-token-2026"),
    "CITIZEN_BEARER_TOKEN": os.environ.get("CITIZEN_BEARER_TOKEN", "sewasetu-citizen-token-2026"),
    "PORT": "8000",
}

for k, v in local_env.items():
    os.environ[k] = v

# Add app directory to sys.path
app_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app")
if app_dir not in sys.path:
    sys.path.insert(0, app_dir)

# Ensure uploads directory exists
os.makedirs(os.environ["UPLOAD_DIR"], exist_ok=True)

if __name__ == "__main__":
    import app as portal
    port = int(os.environ.get("PORT", 8000))
    print(f"[run_local] Starting Sewa Setu on http://127.0.0.1:{port}...")
    portal.app.run(host="0.0.0.0", port=port, debug=False)
