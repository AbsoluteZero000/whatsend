from pathlib import Path

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "sqlite+aiosqlite:///./whatsend.db"
    secret_key: str = "change-me-to-a-random-secret-in-production"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 1440  # 24 hours
    whapi_base_url: str = "https://gate.whapi.cloud"

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = Settings()
BASE_DIR = Path(__file__).resolve().parent.parent
