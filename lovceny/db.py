import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, DeclarativeBase


def _url() -> str:
    url = os.environ.get("DATABASE_URL", "sqlite:///./lovceny.db")
    # Neon/Render give postgres:// or postgresql:// – use psycopg3 driver
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


DATABASE_URL = _url()
engine = create_engine(DATABASE_URL, pool_pre_ping=True, future=True)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def init_db():
    from . import models  # noqa: F401
    Base.metadata.create_all(engine)
