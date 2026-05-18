"""PostGIS (Supabase) connection for the assistant service."""
from __future__ import annotations

from urllib.parse import quote, urlparse, urlunparse

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.config import settings


def _safe_db_url(raw_url: str) -> str:
    """Re-quote credentials so '@' or ':' inside the password don't
    confuse the URL parser. Mirrors backend/services/postgis_service.py."""
    try:
        parsed = urlparse(raw_url)
        if parsed.username is None or parsed.password is None:
            return raw_url
        userinfo = f"{quote(parsed.username, safe='%')}:{quote(parsed.password, safe='%')}"
        netloc = f"{userinfo}@{parsed.hostname}"
        if parsed.port:
            netloc += f":{parsed.port}"
        return urlunparse((
            parsed.scheme, netloc, parsed.path,
            parsed.params, parsed.query, parsed.fragment,
        ))
    except Exception:
        return raw_url


class PostgresDatabase:
    """Singleton wrapper around the SQLAlchemy engine + sessionmaker."""

    _instance: "PostgresDatabase | None" = None

    def __init__(self):
        if not settings.supabase_db_url:
            self.engine = None
            self.SessionLocal = None
            return
        self.engine = create_engine(
            _safe_db_url(settings.supabase_db_url),
            echo=False,
            pool_size=2,
            max_overflow=5,
            pool_pre_ping=True,
            pool_recycle=300,
        )
        self.SessionLocal = sessionmaker(
            autocommit=False, autoflush=False, bind=self.engine,
        )

    @property
    def enabled(self) -> bool:
        return self.engine is not None

    def close(self) -> None:
        if self.engine is not None:
            self.engine.dispose()

    @classmethod
    def get_instance(cls) -> "PostgresDatabase":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance


def get_pg_db() -> PostgresDatabase:
    return PostgresDatabase.get_instance()
