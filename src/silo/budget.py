"""What the agent costs, and a hard daily ceiling on it."""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from silo.settings import Settings


class BudgetExceeded(RuntimeError):
    pass


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0

    def add(self, usage) -> None:
        """Add an Anthropic `Usage` object from one response."""
        self.input_tokens += usage.input_tokens
        self.output_tokens += usage.output_tokens
        self.cache_write_tokens += usage.cache_creation_input_tokens or 0
        self.cache_read_tokens += usage.cache_read_input_tokens or 0

    def cost(self, settings: Settings) -> float:
        per_token = 1 / 1_000_000
        return per_token * (
            self.input_tokens * settings.price_input
            + self.cache_write_tokens * settings.price_cache_write
            + self.cache_read_tokens * settings.price_cache_read
            + self.output_tokens * settings.price_output
        )


class Budget:
    """A per-UTC-day spend ledger in a small JSON file."""

    def __init__(self, path: Path, daily_limit_usd: float):
        self.path = path
        self.daily_limit_usd = daily_limit_usd
        self._lock = threading.Lock()

    def spent_today(self) -> float:
        return self._read().get(_today(), 0.0)

    def check(self) -> None:
        spent = self.spent_today()
        if spent >= self.daily_limit_usd:
            raise BudgetExceeded(
                f"Today's budget of ${self.daily_limit_usd:.2f} is used up (${spent:.4f} spent). "
                "It resets at midnight UTC."
            )

    def record(self, usd: float) -> None:
        with self._lock:
            ledger = self._read()
            ledger[_today()] = ledger.get(_today(), 0.0) + usd
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(ledger, indent=2))

    def _read(self) -> dict[str, float]:
        try:
            return json.loads(self.path.read_text())
        except (FileNotFoundError, ValueError):
            return {}


def _today() -> str:
    return datetime.now(UTC).date().isoformat()
