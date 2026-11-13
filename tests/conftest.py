from pathlib import Path

import pytest

from aftersales_events.config import load_settings
from aftersales_events.fetch import load_raw

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def settings():
    return load_settings(ROOT / "config")


@pytest.fixture
def raw():
    return load_raw(ROOT / "tests" / "fixtures" / "sample_response.json")
