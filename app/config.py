from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = "sqlite+aiosqlite:///./whatsend.db"
    secret_key: str = ""
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 1440
    whapi_base_url: str = "https://gate.whapi.cloud"


settings = Settings()
if not settings.secret_key:
    raise RuntimeError("SECRET_KEY must be set in .env file. See .env.example.")
BASE_DIR = Path(__file__).resolve().parent.parent
