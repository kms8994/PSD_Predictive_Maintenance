"""MySQL 연결 (설계_v2.md 5단계).

접속 정보는 `.env`에서 읽는다(DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD).
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


class Base(DeclarativeBase):
    pass


def database_url() -> str:
    # .env가 없어도 불러오기는 되게 한다(데스크탑에서 DB 없이 더미 생성·사전 학습만 할 때, 2026-11-24). 실제로 DB에 붙을 때 실패한다
    user = os.environ.get("DB_USER", "psd_app")
    password = os.environ.get("DB_PASSWORD", "")
    host = os.environ.get("DB_HOST", "localhost")
    port = os.environ.get("DB_PORT", "3306")
    name = os.environ.get("DB_NAME", "psd")
    return f"mysql+pymysql://{user}:{password}@{host}:{port}/{name}?charset=utf8mb4"


engine = create_engine(database_url(), pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine)


def get_session() -> Session:
    return SessionLocal()
