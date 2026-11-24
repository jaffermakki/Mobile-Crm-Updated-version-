from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

import os

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./crm.db")

# Railway/Supabase give a postgres:// URL; SQLAlchemy needs postgresql://
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

# Name the driver explicitly. A bare "postgresql://" lets SQLAlchemy choose,
# and recent SQLAlchemy releases (2.1+) choose psycopg v3 — but this project
# installs psycopg2-binary (requirements.txt). With an unpinned requirements
# file, the next clean rebuild on Railway/Render would pull the newer
# SQLAlchemy and crash on startup with "No module named 'psycopg'".
if DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg2://", 1)

# SQLite needs check_same_thread=False; Postgres doesn't (and errors if passed it)
connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}

# pool_pre_ping: pings each connection before reuse and transparently
# reconnects if it's gone stale. This matters specifically for Neon's
# free tier, which suspends compute after 5 minutes of inactivity —
# without this, the first request after an idle period can fail with
# "connection already closed" instead of just reconnecting.
engine = create_engine(DATABASE_URL, connect_args=connect_args, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
