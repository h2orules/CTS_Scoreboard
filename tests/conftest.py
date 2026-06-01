import pytest
from pathlib import Path


@pytest.fixture(autouse=True)
def _isolate_settings_file(tmp_path_factory, monkeypatch):
    """Redirect ``CTS_Scoreboard.settings_file`` to a per-session tmp path
    so tests that call ``save_settings()`` (directly or via the /settings
    route) don't churn the repo-tracked ``settings.json``."""
    import CTS_Scoreboard as cts

    tmp = tmp_path_factory.mktemp("settings") / "settings.json"
    monkeypatch.setattr(cts, "settings_file", str(tmp))
    yield


@pytest.fixture
def samples_dir():
    return Path(__file__).resolve().parent.parent / "samples"


@pytest.fixture
def hytek_dir(samples_dir):
    return samples_dir / "HyTek"
