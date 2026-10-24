from pathlib import Path

import pytest

from silo.lake import Lake
from silo.settings import Settings
from silo.tools import Toolbox

# A copy of what `grainflow publish lake` writes, built from Grainflow's dbt
# fixtures: the same tables and manifest as production, a few hundred rows.
FIXTURE_LAKE = Path(__file__).parent / "fixtures" / "lake"


@pytest.fixture(scope="session")
def lake() -> Lake:
    lake = Lake(str(FIXTURE_LAKE))
    lake.load()
    return lake


@pytest.fixture
def settings() -> Settings:
    return Settings(lake_url=str(FIXTURE_LAKE), max_rows=50, query_timeout_s=5)


@pytest.fixture
def toolbox(lake, settings) -> Toolbox:
    return Toolbox(lake, settings)
