from pathlib import Path
from zoneinfo import available_timezones

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = "sqlite+aiosqlite:///./whatsend.db"
    secret_key: str = ""
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 1440
    whapi_base_url: str = "https://gate.whapi.cloud"
    cookie_secure: bool = False
    token_encryption_key: str = ""
    meta_graph_base_url: str = "https://graph.facebook.com"
    meta_graph_api_version: str = "v23.0"
    meta_webhook_verify_token: str = ""
    meta_app_secret: str = ""
    api_keys_enabled: bool = False


settings = Settings()
if not settings.secret_key:
    raise RuntimeError("SECRET_KEY must be set in .env file. See .env.example.")
BASE_DIR = Path(__file__).resolve().parent.parent

# Use the complete IANA database so daylight-saving rules stay accurate worldwide.
# Keep UTC first; the remaining names are alphabetical for the autocomplete list.
TIMEZONE_CHOICES = ["UTC", *sorted(available_timezones() - {"UTC"})]
