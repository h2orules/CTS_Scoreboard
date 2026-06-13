import pytest
from pathlib import Path


@pytest.fixture(autouse=True)
def _isolate_settings_files(tmp_path_factory, monkeypatch):
    """Redirect ``CTS_Scoreboard.settings_file`` and
    ``credentials_store.credentials_file`` to per-session tmp paths so tests
    that call ``save_settings()`` or change credentials (directly or via the
    /settings route) don't churn the repo-tracked ``settings.json`` or leave
    a stray ``credentials.json`` behind."""
    import credentials_store
    import CTS_Scoreboard as cts

    tmp_dir = tmp_path_factory.mktemp("settings")
    monkeypatch.setattr(cts, "settings_file", str(tmp_dir / "settings.json"))
    monkeypatch.setattr(
        credentials_store, "credentials_file", str(tmp_dir / "credentials.json"))
    yield


@pytest.fixture
def samples_dir():
    return Path(__file__).resolve().parent.parent / "samples"


@pytest.fixture
def hytek_dir(samples_dir):
    return samples_dir / "HyTek"
