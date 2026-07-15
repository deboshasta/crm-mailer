# -*- coding: utf-8 -*-
"""Postgres connection for the mailer.
Reads config from environment variables FIRST (for the cloud runner / GitHub Actions),
falling back to ../.env when present (for local use). The only real secret is DB_PASSWORD.
Optional overrides DB_HOST / DB_USER / DB_PORT let the cloud runner use Supabase's IPv4
pooler (GitHub runners are IPv4-only) instead of the direct IPv6 host.
"""
import os
import psycopg2
import psycopg2.extras

_env = {}
_envpath = os.path.join(os.path.dirname(__file__), "..", ".env")
if os.path.exists(_envpath):
    with open(_envpath, encoding="utf-8-sig") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                _env[k.strip()] = v.strip()

def _cfg(k, default=""):
    return os.environ.get(k) or _env.get(k, default)

REF = _cfg("SUPABASE_URL").split("//")[-1].split(".")[0]
PW  = _cfg("DB_PASSWORD")
DB_HOST = _cfg("DB_HOST") or f"db.{REF}.supabase.co"
DB_USER = _cfg("DB_USER") or "postgres"
DB_PORT = int(_cfg("DB_PORT") or "5432")

def connect():
    conn = psycopg2.connect(
        user=DB_USER, password=PW, host=DB_HOST,
        port=DB_PORT, dbname="postgres", sslmode="require",
        connect_timeout=20
    )
    # Auto-parse JSON/JSONB columns to Python dicts (pg8000 did this by default)
    psycopg2.extras.register_default_json(conn)
    psycopg2.extras.register_default_jsonb(conn)
    return conn
