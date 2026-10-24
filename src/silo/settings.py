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

    # The agent. The key is read from ANTHROPIC_API_KEY by the SDK, never here.
    model: str = "claude-haiku-5-5"
    max_steps: int = 8
    # Thinking and the answer share this, so leave room for both.
    max_output_tokens: int = 4096
    # Hard stop on spend per UTC day, across every question.
    daily_budget_usd: float = 0.50
    # USD per million tokens for `model` (prompts up to 100K tokens).
    price_input: float = 0.10
    price_cache_write: float = 0.125
    price_cache_read: float = 0.01
    price_output: float = 0.50
    # Spend ledger and answer traces live here.
    state_dir: str = ".silo"


@lru_cache
def get_settings() -> Settings:
    return Settings()
