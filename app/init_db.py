# -*- coding: utf-8 -*-
"""
Database initialization and seeding script for Sewa Setu.
Automatically checks if the database tables exist and seeds them if fresh.
"""

import io
import os
import sys
import subprocess
import psycopg2

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(BASE_DIR)


def get_connection():
    db_url = os.environ.get("DATABASE_URL") or os.environ.get("DATABASE_PUBLIC_URL")
    if db_url:
        return psycopg2.connect(db_url)
    
    import configparser
    config = configparser.ConfigParser()
    config_path = os.path.join(BASE_DIR, "config", "app.ini")
    if os.path.exists(config_path):
        with open(config_path) as f:
            config.read_file(f)
        return psycopg2.connect(
            host=os.environ.get("DB_HOST", config.get("database", "host", fallback="127.0.0.1")),
            port=os.environ.get("DB_PORT", config.get("database", "port", fallback="5433")),
            dbname=os.environ.get("DB_NAME", config.get("database", "name", fallback="sewasetu")),
            user=os.environ.get("DB_USER", config.get("database", "user", fallback="sewasetu")),
            password=os.environ.get("DB_PASSWORD", config.get("database", "password", fallback="help")),
        )
    return psycopg2.connect(
        host=os.environ.get("DB_HOST", "127.0.0.1"),
        port=os.environ.get("DB_PORT", "5433"),
        dbname=os.environ.get("DB_NAME", "sewasetu"),
        user=os.environ.get("DB_USER", "sewasetu"),
        password=os.environ.get("DB_PASSWORD", "help"),
    )


class CopyStream(io.TextIOBase):
    def __init__(self, file_iter):
        self.file_iter = file_iter

    def readline(self, size=-1):
        try:
            line = next(self.file_iter)
            if line.strip() == r"\.":
                return ""
            return line
        except StopIteration:
            return ""

    def read(self, size=-1):
        return self.readline()


def run_init():
    print("[init_db] Connecting to database...")
    try:
        conn = get_connection()
        conn.autocommit = True
    except Exception as e:
        print(f"[init_db] Could not connect to database: {e}")
        return False

    with conn.cursor() as cur:
        # Check if applications table exists
        cur.execute("SELECT to_regclass('applications');")
        res = cur.fetchone()
        table_exists = res and res[0] is not None
        if table_exists:
            cur.execute("SELECT count(*) FROM applications;")
            count = cur.fetchone()[0]
            if count > 0:
                print(f"[init_db] Database already initialized ({count} applications found).")
                conn.close()
                return True

    print("[init_db] Schema or seed data missing. Initializing...")
    # Find schema.sql
    schema_paths = [
        os.path.join(BASE_DIR, "schema.sql"),
        os.path.join(ROOT_DIR, "app", "schema.sql"),
        "/app/app/schema.sql",
        "/app/schema.sql"
    ]
    schema_file = next((p for p in schema_paths if os.path.exists(p)), None)
    if schema_file:
        print(f"[init_db] Running schema from {schema_file}...")
        with conn.cursor() as cur:
            with open(schema_file, "r", encoding="utf-8") as f:
                cur.execute(f.read())
        print("[init_db] Schema applied successfully.")
    else:
        print("[init_db] Warning: schema.sql not found!")

    # Find seed.sql
    seed_paths = [
        os.path.join(ROOT_DIR, "seed", "seed.sql"),
        os.path.join(BASE_DIR, "seed.sql"),
        "/app/seed/seed.sql",
        "/seed/seed.sql"
    ]
    seed_file = next((p for p in seed_paths if os.path.exists(p)), None)
    if not seed_file:
        print("[init_db] Notice: seed.sql not found, skipping seed load.")
        conn.close()
        return True

    db_url = os.environ.get("DATABASE_URL") or os.environ.get("DATABASE_PUBLIC_URL")

    # Try psql first if available
    psql_success = False
    if db_url:
        try:
            print("[init_db] Attempting fast seed import using psql...")
            ret = subprocess.run(["psql", db_url, "-f", seed_file], capture_output=True, text=True, timeout=120)
            if ret.returncode == 0:
                print("[init_db] Seed loaded successfully via psql.")
                psql_success = True
            else:
                print(f"[init_db] psql exited with code {ret.returncode}: {ret.stderr[:200]}")
        except Exception as e:
            print(f"[init_db] psql invocation skipped: {e}")

    if not psql_success:
        print(f"[init_db] Loading seed from {seed_file} via Python copy_expert...")
        with conn.cursor() as cur:
            with open(seed_file, "r", encoding="utf-8") as f:
                while True:
                    line = f.readline()
                    if not line:
                        break
                    line_s = line.strip()
                    if line_s.startswith("COPY"):
                        copy_cmd = line_s
                        stream = CopyStream(f)
                        cur.copy_expert(copy_cmd, stream)
                    elif line_s.startswith("SELECT setval"):
                        cur.execute(line_s)
        print("[init_db] Seed data loaded successfully via Python!")

    conn.close()
    return True


if __name__ == "__main__":
    success = run_init()
    sys.exit(0 if success else 1)
