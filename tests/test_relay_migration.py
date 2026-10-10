import importlib

from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text

from app.config import settings


def test_relay_migration_preserves_existing_tokens(tmp_path, monkeypatch):
    revision = importlib.import_module("migrations.versions.20261010_0004_relay_api")
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE tokens (id INTEGER PRIMARY KEY, api_token VARCHAR(512) NOT NULL)"))
        connection.execute(text("INSERT INTO tokens (id, api_token) VALUES (1, 'encrypted-existing-token')"))
        monkeypatch.setattr(revision, "op", Operations(MigrationContext.configure(connection)))
        revision.upgrade()
        row = connection.execute(text("SELECT api_token, provider, relay_session_id FROM tokens")).one()
        assert tuple(row) == ("encrypted-existing-token", "whatsapp", None)
        assert "relay_operations" in inspect(connection).get_table_names()
        revision.downgrade()
        assert "provider" not in {column["name"] for column in inspect(connection).get_columns("tokens")}
        assert connection.scalar(text("SELECT api_token FROM tokens")) == "encrypted-existing-token"
    engine.dispose()


def test_complete_migration_chain_on_fresh_database(tmp_path, monkeypatch):
    url = f"sqlite+aiosqlite:///{tmp_path / 'fresh.db'}"
    monkeypatch.setattr(settings, "database_url", url)
    configuration = Config("alembic.ini")
    command.upgrade(configuration, "head")
    engine = create_engine(url.replace("+aiosqlite", ""))
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "20261010_0005"
        assert {"provider", "relay_session_id"} <= {column["name"] for column in inspect(connection).get_columns("tokens")}
        assert "relay_operations" in inspect(connection).get_table_names()
    engine.dispose()
