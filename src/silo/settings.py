from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SILO_", env_file=".env", extra="ignore")

    # Where Grainflow publishes its gold tables: a URL or a local folder holding
    # manifest.json and one Parquet file per table.
    lake_url: str = "https://grainflow-mauve.vercel.app/data/lake"

    max_rows: int = 500
    query_timeout_s: float = 10.0
    # How often to check the lake for a newer build.
    refresh_s: int = 900

    # Host headers the HTTP server answers to, comma separated. Empty means
    # localhost only; set it to the public hostname when deployed.
    allowed_hosts: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()
