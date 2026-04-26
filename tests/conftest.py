import pytest
from pathlib import Path


@pytest.fixture
def samples_dir():
    return Path(__file__).resolve().parent.parent / "samples"


@pytest.fixture
def hytek_dir(samples_dir):
    return samples_dir / "HyTek"
