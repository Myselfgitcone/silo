"""An in-memory DuckDB copy of the published lake.

Grainflow publishes its gold tables as Parquet plus a manifest.json. Silo
downloads them, loads them into memory and then locks the engine down: no file
or network access, no settings changes. The data is a few MB, so a full reload
on every new build is simpler than anything incremental.
"""

from __future__ import annotations

import logging
import math
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb
import httpx

log = logging.getLogger(__name__)


class QueryTimeout(RuntimeError):
    pass


@dataclass(frozen=True)
class Column:
    name: str
    type: str
    description: str


@dataclass(frozen=True)
class Table:
    name: str
    rows: int
    description: str
    columns: tuple[Column, ...]


@dataclass(frozen=True)
class Result:
    columns: list[str]
    rows: list[list[Any]]
    elapsed_ms: int


class Lake:
    def __init__(self, source: str):
        self.source = source.rstrip("/")
        self.tables: dict[str, Table] = {}
        self.generated_at: str | None = None
        self._con: duckdb.DuckDBPyConnection | None = None
        self._lock = threading.Lock()

    @property
    def is_remote(self) -> bool:
        return self.source.startswith(("http://", "https://"))

    def load(self) -> bool:
        """Load the lake if it changed since the last load. Returns True if it did."""
        manifest = self._fetch_json("manifest.json")
        if manifest["generated_at"] == self.generated_at:
            return False

        with tempfile.TemporaryDirectory() as tmp:
            con = duckdb.connect(":memory:")
            tables = {}
            for entry in manifest["tables"]:
                path = self._local_copy(entry["file"], Path(tmp))
                con.execute(f'create table "{entry["name"]}" as select * from read_parquet(?)', [str(path)])
                tables[entry["name"]] = Table(
                    name=entry["name"],
                    rows=entry["rows"],
                    description=entry.get("description", ""),
                    columns=tuple(
                        Column(c["name"], c["type"], c.get("description", "")) for c in entry["columns"]
                    ),
                )
            # Second lock behind the SQL guard: even a query that slipped past it
            # can't read files, reach the network or loosen these settings.
            con.execute("set enable_external_access = false")
            con.execute("set lock_configuration = true")

        with self._lock:
            old, self._con = self._con, con
            self.tables = tables
            self.generated_at = manifest["generated_at"]
        if old is not None:
            old.close()
        log.info("loaded %d tables from %s (built %s)", len(tables), self.source, self.generated_at)
        return True

    def execute(self, sql: str, timeout_s: float) -> Result:
        with self._lock:
            if self._con is None:
                raise RuntimeError("the lake hasn't been loaded")
            cursor = self._con.cursor()

        timer = threading.Timer(timeout_s, cursor.interrupt)
        started = time.perf_counter()
        timer.start()
        try:
            relation = cursor.execute(sql)
            columns = [d[0] for d in relation.description]
            rows = [[_plain(v) for v in row] for row in relation.fetchall()]
        except duckdb.InterruptException:
            raise QueryTimeout(f"The query ran longer than {timeout_s:g}s and was stopped.") from None
        finally:
            timer.cancel()
            cursor.close()
        return Result(columns, rows, round((time.perf_counter() - started) * 1000))

    def _fetch_json(self, name: str) -> dict[str, Any]:
        if self.is_remote:
            response = httpx.get(f"{self.source}/{name}", timeout=30, follow_redirects=True)
            response.raise_for_status()
            return response.json()
        import json

        return json.loads((Path(self.source) / name).read_text())

    def _local_copy(self, name: str, tmp: Path) -> Path:
        if not self.is_remote:
            return Path(self.source) / name
        target = tmp / name
        with httpx.stream("GET", f"{self.source}/{name}", timeout=60, follow_redirects=True) as response:
            response.raise_for_status()
            with target.open("wb") as file:
                for chunk in response.iter_bytes():
                    file.write(chunk)
        return target


def _plain(value: Any) -> Any:
    """Make a DuckDB value safe to put in JSON."""
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    return value
