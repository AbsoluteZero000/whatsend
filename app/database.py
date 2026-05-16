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
        if "skip_count" not in jcols:
            conn.execute(text("ALTER TABLE jobs ADD COLUMN skip_count INTEGER DEFAULT 0"))
    except Exception:
        pass

    cols = [c["name"] for c in inspector.get_columns("users")]
    if "lang" not in cols:
        conn.execute(text("ALTER TABLE users ADD COLUMN lang VARCHAR(2) DEFAULT 'en'"))
        conn.execute(text("UPDATE users SET lang = 'en' WHERE lang IS NULL"))

    _fix_cron_dow_migration(conn)


def _fix_cron_dow(v: str) -> str:
    parts = v.split()
    if len(parts) != 5:
        return v
    dow = parts[4]
    if dow == "*":
        return v
    fixed: list[str] = []
    for token in dow.split(","):
        if "-" in token:
            a, b = token.split("-", 1)
            fixed.append(f"{(int(a) - 1) % 7}-{(int(b) - 1) % 7}")
        else:
            fixed.append(str((int(token) - 1) % 7))
    parts[4] = ",".join(fixed)
    return " ".join(parts)


def _fix_cron_dow_migration(conn):
    from sqlalchemy import text
    rows = conn.execute(text("SELECT id, trigger_value FROM jobs WHERE trigger_type = 'cron'")).fetchall()
    for row in rows:
        fixed = _fix_cron_dow(row.trigger_value)
        if fixed != row.trigger_value:
            conn.execute(text("UPDATE jobs SET trigger_value = :val WHERE id = :id"), {"val": fixed, "id": row.id})
