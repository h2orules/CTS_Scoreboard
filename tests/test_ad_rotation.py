"""Tests for the ad image upload and rotation feature."""
import io
import os
import pytest

import CTS_Scoreboard as cts
from CTS_Scoreboard import app, settings


@pytest.fixture
def logged_in_client():
    app.config["TESTING"] = True
    with app.test_client() as c:
        c.post("/login", data={
            "username": settings["username"],
            "password": settings["password"],
        })
        yield c


@pytest.fixture
def clean_ad_state(tmp_path, monkeypatch):
    """Snapshot/restore ad-related global state around each test.

    Also redirect the on-disk ad image directory to a tmp path so uploads
    don't pollute the repo's static/ad/ folder.
    """
    original_images = list(settings.get('ad_images') or [])
    original_interval = settings.get('ad_rotation_interval', 30)
    original_index = cts._ad_rotation_index
    original_running = cts._ad_rotation_running

    # Point the route's AD_DIR at a tmp location by symlinking would be heavy;
    # instead, accept that uploads land in the real static/ad/ and clean up
    # after ourselves at the end of the test.
    settings['ad_images'] = []
    settings['ad_rotation_interval'] = 30
    cts._ad_rotation_index = 0
    cts._ad_rotation_running = False

    yield

    # Cleanup: remove any files left behind from upload tests.
    real_ad_dir = os.path.join(os.path.dirname(cts.__file__), 'static', 'ad')
    try:
        for entry in (settings.get('ad_images') or []):
            if isinstance(entry, dict) and entry.get('filename'):
                fp = os.path.join(real_ad_dir, entry['filename'])
                if os.path.exists(fp):
                    try:
                        os.unlink(fp)
                    except OSError:
                        pass
    except Exception:
        pass

    settings['ad_images'] = original_images
    settings['ad_rotation_interval'] = original_interval
    cts._ad_rotation_index = original_index
    cts._ad_rotation_running = original_running
    # Persist so the on-disk settings.json reflects the restored state and
    # subsequent test runs don't see test residue.
    try:
        cts.save_settings()
    except Exception:
        pass


# --- _enabled_ad_indices / _update_ad_rotation ------------------------------


def test_enabled_ad_indices_filters_disabled(clean_ad_state):
    settings['ad_images'] = [
        {'filename': 'a.jpg', 'enabled': True},
        {'filename': 'b.jpg', 'enabled': False},
        {'filename': 'c.jpg', 'enabled': True},
    ]
    assert cts._enabled_ad_indices() == [0, 2]


def test_update_ad_rotation_no_timer_with_zero_images(clean_ad_state):
    settings['ad_images'] = []
    cts._update_ad_rotation()
    assert cts._ad_rotation_running is False


def test_update_ad_rotation_no_timer_with_one_image(clean_ad_state):
    settings['ad_images'] = [{'filename': 'only.jpg', 'enabled': True}]
    cts._update_ad_rotation()
    assert cts._ad_rotation_running is False


def test_update_ad_rotation_no_timer_when_only_one_enabled(clean_ad_state):
    settings['ad_images'] = [
        {'filename': 'a.jpg', 'enabled': True},
        {'filename': 'b.jpg', 'enabled': False},
    ]
    cts._update_ad_rotation()
    assert cts._ad_rotation_running is False


def test_update_ad_rotation_clamps_index(clean_ad_state):
    settings['ad_images'] = [
        {'filename': 'a.jpg', 'enabled': False},
        {'filename': 'b.jpg', 'enabled': True},
    ]
    cts._ad_rotation_index = 0  # 0 is disabled
    cts._update_ad_rotation()
    assert cts._ad_rotation_index == 1


def test_update_ad_rotation_resets_when_none_enabled(clean_ad_state):
    settings['ad_images'] = [
        {'filename': 'a.jpg', 'enabled': False},
        {'filename': 'b.jpg', 'enabled': False},
    ]
    cts._ad_rotation_index = 1
    cts._update_ad_rotation()
    assert cts._ad_rotation_index == 0
    assert cts._ad_rotation_running is False


# --- Route: upload / remove / toggle / reorder ------------------------------


def _png_bytes():
    """Return a small valid PNG (Pillow-decodable)."""
    from PIL import Image
    buf = io.BytesIO()
    Image.new('RGBA', (2, 2), color=(255, 0, 0, 255)).save(buf, format='PNG')
    return buf.getvalue()


def test_upload_appends_to_ad_images(logged_in_client, clean_ad_state):
    data = {
        'ad_images': (io.BytesIO(_png_bytes()), 'sample.png'),
    }
    resp = logged_in_client.post('/settings', data=data,
                                 content_type='multipart/form-data')
    assert resp.status_code == 200
    ads = settings.get('ad_images') or []
    assert len(ads) == 1
    assert ads[0]['filename'].endswith('.png')
    assert ads[0]['enabled'] is True


def test_upload_rejects_unsupported_extension(logged_in_client, clean_ad_state):
    data = {
        'ad_images': (io.BytesIO(b'not an image'), 'evil.exe'),
    }
    resp = logged_in_client.post('/settings', data=data,
                                 content_type='multipart/form-data')
    assert resp.status_code == 200
    assert (settings.get('ad_images') or []) == []


def test_upload_multiple_at_once(logged_in_client, clean_ad_state):
    data = {
        'ad_images': [
            (io.BytesIO(_png_bytes()), 'one.png'),
            (io.BytesIO(_png_bytes()), 'two.png'),
        ],
    }
    resp = logged_in_client.post('/settings', data=data,
                                 content_type='multipart/form-data')
    assert resp.status_code == 200
    ads = settings.get('ad_images') or []
    assert len(ads) == 2


def test_remove_deletes_file_and_entry(logged_in_client, clean_ad_state):
    # Seed one upload.
    logged_in_client.post('/settings',
                          data={'ad_images': (io.BytesIO(_png_bytes()), 'rm.png')},
                          content_type='multipart/form-data')
    ads = settings.get('ad_images') or []
    assert len(ads) == 1
    fname = ads[0]['filename']
    ad_dir = os.path.join(os.path.dirname(cts.__file__), 'static', 'ad')
    assert os.path.exists(os.path.join(ad_dir, fname))
    # Remove via form.
    resp = logged_in_client.post('/settings', data={
        'ad_form': '1',
        'ad_rotation_interval': '30',
        'ad_remove_0': '1',
    })
    assert resp.status_code == 200
    assert (settings.get('ad_images') or []) == []
    assert not os.path.exists(os.path.join(ad_dir, fname))


def test_toggle_enabled_off_via_form(logged_in_client, clean_ad_state):
    settings['ad_images'] = [
        {'filename': 'x.jpg', 'enabled': True},
        {'filename': 'y.jpg', 'enabled': True},
    ]
    # Submit ad form with only ad_enabled_0 checked.
    resp = logged_in_client.post('/settings', data={
        'ad_form': '1',
        'ad_rotation_interval': '30',
        'ad_enabled_0': 'on',
    })
    assert resp.status_code == 200
    ads = settings.get('ad_images') or []
    assert ads[0]['enabled'] is True
    assert ads[1]['enabled'] is False


def test_reorder_down_swaps_neighbors(logged_in_client, clean_ad_state):
    settings['ad_images'] = [
        {'filename': 'a.jpg', 'enabled': True},
        {'filename': 'b.jpg', 'enabled': True},
        {'filename': 'c.jpg', 'enabled': True},
    ]
    resp = logged_in_client.post('/settings', data={
        'ad_form': '1',
        'ad_rotation_interval': '30',
        'ad_down_0': '1',
        'ad_enabled_0': 'on',
        'ad_enabled_1': 'on',
        'ad_enabled_2': 'on',
    })
    assert resp.status_code == 200
    ads = settings.get('ad_images') or []
    assert [a['filename'] for a in ads] == ['b.jpg', 'a.jpg', 'c.jpg']


def test_reorder_up_swaps_neighbors(logged_in_client, clean_ad_state):
    settings['ad_images'] = [
        {'filename': 'a.jpg', 'enabled': True},
        {'filename': 'b.jpg', 'enabled': True},
    ]
    resp = logged_in_client.post('/settings', data={
        'ad_form': '1',
        'ad_rotation_interval': '30',
        'ad_up_1': '1',
        'ad_enabled_0': 'on',
        'ad_enabled_1': 'on',
    })
    assert resp.status_code == 200
    ads = settings.get('ad_images') or []
    assert [a['filename'] for a in ads] == ['b.jpg', 'a.jpg']


def test_interval_validation(logged_in_client, clean_ad_state):
    settings['ad_images'] = [{'filename': 'a.jpg', 'enabled': True}]
    # Out-of-range falls back to 30.
    resp = logged_in_client.post('/settings', data={
        'ad_form': '1',
        'ad_rotation_interval': '999',
        'ad_enabled_0': 'on',
    })
    assert resp.status_code == 200
    assert settings['ad_rotation_interval'] == 30
    # Non-multiple-of-5 also falls back.
    logged_in_client.post('/settings', data={
        'ad_form': '1',
        'ad_rotation_interval': '7',
        'ad_enabled_0': 'on',
    })
    assert settings['ad_rotation_interval'] == 30
    # Valid value accepted.
    logged_in_client.post('/settings', data={
        'ad_form': '1',
        'ad_rotation_interval': '15',
        'ad_enabled_0': 'on',
    })
    assert settings['ad_rotation_interval'] == 15


def test_settings_page_renders_ad_section(logged_in_client, clean_ad_state):
    settings['ad_images'] = [{'filename': 'shown.jpg', 'enabled': True}]
    resp = logged_in_client.get('/settings')
    assert resp.status_code == 200
    body = resp.data.decode()
    assert 'Ad Display' in body
    assert 'shown.jpg' in body
    assert 'ad_rotation_interval' in body
    assert 'max-width:150px' in body
