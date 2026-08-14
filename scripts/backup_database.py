#!/usr/bin/env python3
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.engine import make_url

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings


def main() -> None:
    url = make_url(settings.database_url.replace("+aiosqlite", ""))
    if url.get_backend_name() != "sqlite" or not url.database:
        raise SystemExit("backup_database.py supports SQLite deployments only")

    source_path = Path(url.database).resolve()
    if not source_path.is_file():
        raise SystemExit(f"database not found: {source_path}")

    backup_dir = Path(os.getenv("BACKUP_DIR", str(source_path.parent / "backups"))).resolve()
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    destination = backup_dir / f"whatsend-{timestamp}.db"

    with sqlite3.connect(source_path) as source, sqlite3.connect(destination) as target:
        source.backup(target)

    retention = max(1, int(os.getenv("BACKUP_RETENTION", "14")))
    backups = sorted(backup_dir.glob("whatsend-*.db"), key=lambda path: path.stat().st_mtime, reverse=True)
    for expired in backups[retention:]:
        expired.unlink()

    print(destination)


if __name__ == "__main__":
    main()
