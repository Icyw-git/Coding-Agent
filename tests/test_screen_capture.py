"""screen_capture：区域解析、runner 注入、落盘/保留策略（不碰真实屏幕）。"""
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import screen_capture

PNG = b'\x89PNG\r\n\x1a\n' + b'fake-png-body'


def _capture_runner(meta=None, *, data=PNG, returncode=0, stderr=b''):
    calls = []

    def runner(script, env, timeout):
        calls.append({'script': script, 'env': env, 'timeout': timeout})
        if env['HARNESS_SCREEN_OUT']:
            Path(env['HARNESS_SCREEN_OUT']).write_bytes(data)
        payload = {
            'ok': True,
            'width': 800,
            'height': 600,
            'screen': env['HARNESS_SCREEN_MODE'],
            'rect': [0, 0, 800, 600],
            'region': env['HARNESS_SCREEN_REGION'] or None,
            'screens': [{'name': '\\\\.\\DISPLAY1', 'primary': True,
                         'bounds': [0, 0, 800, 600]}],
        }
        if meta:
            payload.update(meta)
        if payload.get('_write_meta', True):
            payload.pop('_write_meta', None)
            Path(env['HARNESS_SCREEN_META']).write_text(
                json.dumps(payload), encoding='utf-8')
        return SimpleNamespace(returncode=returncode, stdout=b'', stderr=stderr)

    return runner, calls


def test_capture_passes_mode_region_and_delay_to_powershell():
    runner, calls = _capture_runner()

    result = screen_capture.capture(
        screen='all', region='10,20,300,200', delay_ms=250, runner=runner)

    env = calls[0]['env']
    assert env['HARNESS_SCREEN_MODE'] == 'all'
    assert env['HARNESS_SCREEN_REGION'] == '10,20,300,200'
    assert env['HARNESS_SCREEN_DELAY_MS'] == '250'
    assert result.png == PNG
    assert (result.width, result.height) == (800, 600)
    assert result.region == (10, 20, 300, 200)
    assert result.screens[0]['primary'] is True


@pytest.mark.parametrize('region', [
    '1,2,3', 'a,b,c,d', '-1,0,10,10', '0,0,0,10', '0,0,10,-1',
])
def test_parse_region_rejects_bad_values(region):
    with pytest.raises(screen_capture.ScreenError):
        screen_capture.parse_region(region)


def test_parse_region_accepts_sequence_and_empty():
    assert screen_capture.parse_region([1, 2, 3, 4]) == (1, 2, 3, 4)
    assert screen_capture.parse_region('') is None
    assert screen_capture.parse_region(None) is None


def test_capture_rejects_unknown_screen_mode():
    with pytest.raises(screen_capture.ScreenError):
        screen_capture.capture(screen='secondary', runner=lambda *a: None)


def test_capture_surfaces_structured_powershell_error():
    runner, _ = _capture_runner(
        {'ok': False, 'error': 'capture_failed', 'message': 'no desktop'})

    with pytest.raises(screen_capture.ScreenError, match='no desktop'):
        screen_capture.capture(runner=runner)


def test_capture_without_metadata_reports_exit_code():
    runner, _ = _capture_runner(
        {'_write_meta': False}, returncode=2, stderr=b'boom')

    with pytest.raises(screen_capture.ScreenError, match='boom'):
        screen_capture.capture(runner=runner)


def test_capture_rejects_non_png_payload():
    runner, _ = _capture_runner(data=b'not-a-png')

    with pytest.raises(screen_capture.ScreenError, match='PNG'):
        screen_capture.capture(runner=runner)


def test_save_png_is_content_addressed_and_reuses_fresh_file(tmp_path):
    first = screen_capture.save_png(PNG, tmp_path)
    second = screen_capture.save_png(PNG, tmp_path)

    assert first == second
    assert first.name.endswith('.png')
    assert first.read_bytes() == PNG

    old = time.time() - 10
    os.utime(first, (old, old))
    fresh = screen_capture.save_png(PNG, tmp_path, keep_ms=5000)
    assert fresh == first
    assert first.stat().st_mtime > old + 5


def test_latest_png_picks_newest_and_ignores_directories(tmp_path):
    older = tmp_path / 'older.png'
    newer = tmp_path / 'newer.png'
    older.write_bytes(PNG)
    newer.write_bytes(PNG)
    (tmp_path / 'nested.png').mkdir()
    old = time.time() - 100
    os.utime(older, (old, old))

    assert screen_capture.latest_png(tmp_path) == newer
    assert screen_capture.latest_png(tmp_path / 'missing') is None


def test_prune_and_stats_respect_retention(tmp_path):
    old = tmp_path / 'old.png'
    new = tmp_path / 'new.png'
    old.write_bytes(PNG)
    new.write_bytes(PNG)
    now = time.time()
    os.utime(old, (now - 8 * 86400, now - 8 * 86400))
    os.utime(new, (now, now))

    assert screen_capture.prune(tmp_path, days=7, now=now) == 1
    remaining = screen_capture.stats(tmp_path)
    assert not old.exists()
    assert remaining['files'] == 1
    assert remaining['bytes'] == len(PNG)
    assert remaining['newest'] == pytest.approx(now, abs=5)
    assert screen_capture.stats(tmp_path / 'missing')['files'] == 0
