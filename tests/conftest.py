from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from src.classify import load_classifier

FIXTURES = Path(__file__).parent / "fixtures"
ROOT = Path(__file__).resolve().parent.parent


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture(scope="session")
def classifier():
    return load_classifier(ROOT / "config" / "taxonomy.yaml")


@pytest.fixture(scope="session")
def config() -> dict:
    return yaml.safe_load((ROOT / "config" / "sources.yaml").read_text())
