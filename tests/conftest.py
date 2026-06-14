import tempfile
import pytest
from pathlib import Path

# Point the per-install secret key at a throwaway location *before* any test
# imports CTS_Scoreboard (whose module-level app config generates the key).
# conftest is imported by pytest ahead of the test modules, so this keeps the
# import-time key generation out of the repo directory.
import credentials_store as _credentials_store

_credentials_store.secret_key_file = str(
    Path(tempfile.mkdtemp(prefix="cts_secret_")) / "secret_key")


@pytest.fixture(autouse=True)
def _isolate_settings_files(tmp_path_factory, monkeypatch):
    """Redirect ``CTS_Scoreboard.settings_file``,
    ``credentials_store.credentials_file`` and
    ``credentials_store.secret_key_file`` to per-session tmp paths so tests
    that call ``save_settings()``, change credentials, or generate a secret
    key (directly or via the /settings route) don't churn the repo-tracked
    ``settings.json`` or leave a stray ``credentials.json`` / ``secret_key``
    behind."""
    import credentials_store
    import CTS_Scoreboard as cts

    tmp_dir = tmp_path_factory.mktemp("settings")
    monkeypatch.setattr(cts, "settings_file", str(tmp_dir / "settings.json"))
    monkeypatch.setattr(
        credentials_store, "credentials_file", str(tmp_dir / "credentials.json"))
    monkeypatch.setattr(
        credentials_store, "secret_key_file", str(tmp_dir / "secret_key"))
    yield


@pytest.fixture
def samples_dir():
    return Path(__file__).resolve().parent.parent / "samples"


@pytest.fixture
def hytek_dir(samples_dir):
    return samples_dir / "HyTek"
