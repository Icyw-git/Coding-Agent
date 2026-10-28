"""截图坐标系闭环：图像像素 → 屏幕坐标。

「截了图再用 OCR 找文字，然后点它」这条链路只有在原点信息传下去的时候才成立。
这里锁住三件事：原点被记下来、被算进去、被清理掉。
"""
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

import screen_capture
import tool_screen

PNG = b'\x89PNG\r\n\x1a\n' + b'fake-png-body'


def _capture_runner(rect=(120, 80, 800, 600)):
    def runner(script, env, timeout):
        if env['HARNESS_SCREEN_OUT']:
            Path(env['HARNESS_SCREEN_OUT']).write_bytes(PNG)
        payload = {
            'ok': True, 'width': 800, 'height': 600,
            'screen': env['HARNESS_SCREEN_MODE'],
            'rect': list(rect),
            'region': env['HARNESS_SCREEN_REGION'] or None,
            'screens': [{'name': '\\\\.\\DISPLAY1', 'primary': True,
                         'bounds': [0, 0, 800, 600]}],
        }
        Path(env['HARNESS_SCREEN_META']).write_text(
            json.dumps(payload), encoding='utf-8')
        return SimpleNamespace(returncode=0, stdout=b'', stderr=b'')

    return runner


def _ocr_runner(text='保存', lines=None):
    if lines is None:
        lines = [{'text': text, 'bbox': [10, 20, 100, 18], 'words': []}]

    def runner(script, env, timeout):
        Path(env['HARNESS_OCR_META']).write_text(json.dumps({
            'ok': True, 'language': 'zh-Hans-CN', 'width': 800, 'height': 600,
            'region': env['HARNESS_OCR_REGION'] or None,
            'text': text, 'lines': lines,
        }), encoding='utf-8')
        return SimpleNamespace(returncode=0, stdout=b'', stderr=b'')

    return runner


# ---------- capture 记下原点 ----------

def test_capture_records_screen_origin():
    shot = screen_capture.capture(runner=_capture_runner(rect=(120, 80, 800, 600)))
    assert shot.origin == (120, 80)


def test_capture_defaults_origin_when_meta_missing_rect():
    def runner(script, env, timeout):
        Path(env['HARNESS_SCREEN_OUT']).write_bytes(PNG)
        Path(env['HARNESS_SCREEN_META']).write_text(json.dumps({
            'ok': True, 'width': 10, 'height': 10, 'screen': 'primary',
            'screens': []}), encoding='utf-8')
        return SimpleNamespace(returncode=0, stdout=b'', stderr=b'')

    assert screen_capture.capture(runner=runner).origin == (0, 0)


# ---------- 边车文件 ----------

def test_save_png_writes_sidecar(tmp_path):
    target = screen_capture.save_png(PNG, tmp_path, meta={
        'origin': [120, 80], 'width': 800, 'height': 600})
    sidecar = target.with_suffix('.json')
    assert sidecar.is_file()
    data = json.loads(sidecar.read_text(encoding='utf-8'))
    assert data['origin'] == [120, 80]
    assert data['width'] == 800
    assert data['sha1'] == target.stem


def test_save_png_without_meta_leaves_no_sidecar(tmp_path):
    target = screen_capture.save_png(PNG, tmp_path)
    assert not target.with_suffix('.json').exists()
    assert screen_capture.load_meta(target) is None


def test_load_meta_tolerates_broken_sidecar(tmp_path):
    target = screen_capture.save_png(PNG, tmp_path, meta={'origin': [1, 2]})
    target.with_suffix('.json').write_text('{not json', encoding='utf-8')
    assert screen_capture.load_meta(target) is None


def test_prune_removes_sidecar_with_the_png(tmp_path):
    target = screen_capture.save_png(PNG, tmp_path, meta={'origin': [0, 0]})
    old = time.time() - 10 * 86400
    os.utime(target, (old, old))
    removed = screen_capture.prune(tmp_path, days=7)
    assert removed == 1
    assert not target.exists()
    assert not target.with_suffix('.json').exists()


# ---------- OCR 输出可点的屏幕坐标 ----------

def test_ocr_adds_screen_coordinates_when_sidecar_exists(tmp_path):
    target = screen_capture.save_png(PNG, tmp_path, meta={'origin': [120, 80]})
    result = tool_screen.ocr(target.name, directory=tmp_path, runner=_ocr_runner())
    # bbox 10,20,100,18 -> 中心 (60,29) -> 屏幕 (180,109)
    assert '@(180,109)' in result
    assert '截图原点 (120,80)' in result
    assert 'desktop_action' in result
    result.encode('gbk')


def test_ocr_without_sidecar_says_pixels_are_image_only(tmp_path):
    target = tmp_path / 'shot.png'
    target.write_bytes(PNG)
    result = tool_screen.ocr('shot.png', directory=tmp_path, runner=_ocr_runner())
    assert '没有留下坐标信息' in result
    assert '@(' not in result
    assert '[10,20,100,18]' in result


def test_ocr_explains_coordinate_space_for_region_crop(tmp_path):
    target = screen_capture.save_png(PNG, tmp_path, meta={'origin': [0, 0]})
    lines = [{'text': 'hi', 'bbox': [0, 0, 10, 10], 'words': []}]
    result = tool_screen.ocr(target.name, region='5,5,100,50',
                             directory=tmp_path, runner=_ocr_runner(lines=lines))
    assert '@(5,5)' in result


def test_capture_text_states_the_coordinate_space(tmp_path):
    result = tool_screen.capture(directory=tmp_path,
                                 runner=_capture_runner(rect=(120, 80, 800, 600)))
    assert '坐标系：绝对屏幕像素' in result.text
    assert '(120,80)' in result.text
    result.text.encode('gbk')


def test_capture_writes_sidecar_next_to_png(tmp_path):
    result = tool_screen.capture(directory=tmp_path, runner=_capture_runner())
    saved = Path(result.images[0]['path'])
    meta = screen_capture.load_meta(saved)
    assert meta is not None
    assert meta['origin'] == [120, 80]
    assert meta['width'] == 800


def test_line_center_handles_empty_bbox():
    assert tool_screen._line_center({'bbox': []}) is None
    assert tool_screen._line_center({'bbox': [0, 0, 4, 4]}) == (2, 2)
