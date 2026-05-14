from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import settings

engine = create_async_engine(settings.database_url, echo=False)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def get_db():
    async with async_session() as session:
        try:
            yield session
        finally:
            await session.close()


async def create_tables():
    async with engine.begin() as conn:
        from app.models import user, token, job, log
        await conn.run_sync(Base.metadata.create_all)

    async with engine.begin() as conn:
        await conn.run_sync(_migrate)


def _migrate(conn):
    from sqlalchemy import inspect, text
    inspector = inspect(conn)
    cols = [c["name"] for c in inspector.get_columns("users")]
    if "timezone" not in cols:
        conn.execute(text("ALTER TABLE users ADD COLUMN timezone VARCHAR(64) DEFAULT 'UTC'"))
        conn.execute(text("UPDATE users SET timezone = 'UTC' WHERE timezone IS NULL"))

    try:
        jcols = [c["name"] for c in inspector.get_columns("jobs")]
        if "group_name" not in jcols:
            conn.execute(text("ALTER TABLE jobs ADD COLUMN group_name VARCHAR(255)"))
    except Exception:
        pass
