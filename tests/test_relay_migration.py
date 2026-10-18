import importlib

from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text

from app.config import settings


def test_attachment_filename_migration_preserves_existing_jobs(tmp_path, monkeypatch):
    revision = importlib.import_module("migrations.versions.20261011_0007_attachment_filename")
    engine = create_engine(f"sqlite:///{tmp_path / 'attachment-upgrade.db'}")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE jobs (id INTEGER PRIMARY KEY, image_path TEXT)"))
        connection.execute(text("INSERT INTO jobs VALUES (1, '/data/uploads/old.pdf')"))
        monkeypatch.setattr(revision, "op", Operations(MigrationContext.configure(connection)))
        revision.upgrade()
        revision.upgrade()  # Fresh baselines may already contain the new column.
        assert connection.execute(text("SELECT image_path,image_filename FROM jobs WHERE id=1")).one() == ('/data/uploads/old.pdf', None)
        revision.downgrade()
        assert connection.scalar(text("SELECT image_path FROM jobs WHERE id=1")) == '/data/uploads/old.pdf'
    engine.dispose()


def test_native_migration_preserves_links_and_retires_external_credentials(tmp_path, monkeypatch):
    revision = importlib.import_module("migrations.versions.20261011_0006_native_whatsapp")
    engine = create_engine(f"sqlite:///{tmp_path / 'native-upgrade.db'}")
    with engine.begin() as connection:
        for sql in [
            "CREATE TABLE tokens (id INTEGER PRIMARY KEY, provider TEXT, api_token TEXT, relay_session_id TEXT, relay_base_url TEXT)",
            "CREATE TABLE jobs (id INTEGER PRIMARY KEY, token_id INTEGER, status TEXT)",
            "CREATE TABLE relay_operations (status TEXT, response TEXT)",
            "CREATE TABLE delivery_attempts (status TEXT, response TEXT, relay_token_id INTEGER)",
            "INSERT INTO tokens VALUES (3, 'relay', 'old-encrypted-key', NULL, 'https://old-relay.test')",
            "INSERT INTO tokens VALUES (4, 'whatsapp', 'whapi-key', NULL, NULL)",
            "INSERT INTO jobs VALUES (5, 3, 'queued')",
            "INSERT INTO relay_operations VALUES ('queued', 'old-ack')",
            "INSERT INTO delivery_attempts VALUES ('queued', 'old-ack', 3)",
        ]:
            connection.execute(text(sql))
        monkeypatch.setattr(revision, "op", Operations(MigrationContext.configure(connection)))
        revision.upgrade()
        token = connection.execute(text("SELECT provider,api_token,relay_session_id,relay_base_url FROM tokens WHERE id=3")).one()
        assert token.provider == "native" and token.api_token == "" and token.relay_session_id and token.relay_base_url is None
        assert connection.scalar(text("SELECT api_token FROM tokens WHERE id=4")) == "whapi-key"
        assert connection.execute(text("SELECT token_id,status FROM jobs WHERE id=5")).one() == (3, "unknown")
        assert connection.scalar(text("SELECT status FROM delivery_attempts")) == "unknown"
        assert connection.scalar(text("SELECT status FROM relay_operations")) == "unknown"
    engine.dispose()


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
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "20261018_0010"
        assert "session_version" in {column["name"] for column in inspect(connection).get_columns("users")}
        assert "image_filename" in {column["name"] for column in inspect(connection).get_columns("jobs")}
        assert {"provider", "relay_session_id"} <= {column["name"] for column in inspect(connection).get_columns("tokens")}
        assert "relay_operations" in inspect(connection).get_table_names()
        assert "relay_message_id" in {column["name"] for column in inspect(connection).get_columns("delivery_attempts")}
    engine.dispose()


def test_recipient_list_migration_on_existing_installation(tmp_path, monkeypatch):
    revision = importlib.import_module('migrations.versions.20261013_0008_recipient_lists')
    engine = create_engine(f"sqlite:///{tmp_path / 'lists.db'}")
    with engine.begin() as connection:
        connection.execute(text('PRAGMA foreign_keys=ON'))
        connection.execute(text('CREATE TABLE users (id INTEGER PRIMARY KEY)'))
        connection.execute(text('CREATE TABLE tokens (id INTEGER PRIMARY KEY)'))
        connection.execute(text('INSERT INTO users VALUES (7)'))
        connection.execute(text('INSERT INTO tokens VALUES (3)'))
        monkeypatch.setattr(revision, 'op', Operations(MigrationContext.configure(connection)))
        revision.upgrade()
        connection.execute(text("INSERT INTO recipient_lists(user_id,token_id,name,recipients) VALUES(7,3,'All branches','[]')"))
        revision.upgrade()  # Baseline/current metadata may already have created it.
        assert connection.scalar(text('SELECT count(*) FROM recipient_lists')) == 1
        connection.execute(text('DELETE FROM tokens WHERE id=3'))
        assert connection.scalar(text('SELECT count(*) FROM recipient_lists')) == 0
        assert connection.scalar(text('SELECT count(*) FROM users')) == 1
        revision.downgrade()
        assert 'recipient_lists' not in inspect(connection).get_table_names()
    engine.dispose()


def test_series_migration_preserves_existing_schedules(tmp_path, monkeypatch):
    revision = importlib.import_module('migrations.versions.20261014_0009_message_series')
    engine = create_engine(f"sqlite:///{tmp_path / 'series-legacy.db'}")
    with engine.begin() as connection:
        connection.execute(text('CREATE TABLE users (id INTEGER PRIMARY KEY)'))
        connection.execute(text('CREATE TABLE tokens (id INTEGER PRIMARY KEY)'))
        connection.execute(text('CREATE TABLE jobs (id INTEGER PRIMARY KEY,message TEXT,status TEXT)'))
        connection.execute(text("INSERT INTO jobs VALUES (3,'Keep repeating','active')"))
        connection.execute(text('CREATE TABLE delivery_attempts (id INTEGER PRIMARY KEY,job_id INTEGER)'))
        monkeypatch.setattr(revision,'op',Operations(MigrationContext.configure(connection)))
        revision.upgrade();revision.upgrade()
        row=connection.execute(text('SELECT message,status,message_mode,series_cursor,series_total,send_count FROM jobs WHERE id=3')).one()
        assert tuple(row)==('Keep repeating','active','repeat',0,0,0)
        assert {'series_entries','series_attachments'} <= set(inspect(connection).get_table_names())
        revision.downgrade()
        assert connection.scalar(text('SELECT message FROM jobs WHERE id=3'))=='Keep repeating'
        assert 'message_mode' not in {column['name'] for column in inspect(connection).get_columns('jobs')}
    engine.dispose()
