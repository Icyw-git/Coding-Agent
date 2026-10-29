"""tool_screen：落盘、多模态引用、默认最新截图与目录边界。"""
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

import tool_documents
import tool_screen

PNG = b'\x89PNG\r\n\x1a\n' + b'screenshot'


def _capture_runner(width=800, height=600):
    def runner(script, env, timeout):
        Path(env['HARNESS_SCREEN_OUT']).write_bytes(PNG)
        Path(env['HARNESS_SCREEN_META']).write_text(json.dumps({
            'ok': True, 'width': width, 'height': height,
            'screen': env['HARNESS_SCREEN_MODE'],
            'rect': [0, 0, width, height],
            'region': env['HARNESS_SCREEN_REGION'] or None,
            'screens': [{'name': 'DISPLAY1', 'primary': True,
                         'bounds': [0, 0, width, height]}],
        }), encoding='utf-8')
        return SimpleNamespace(returncode=0, stdout=b'', stderr=b'')

    return runner


def _ocr_runner(text='hello', lines=None):
    def runner(script, env, timeout):
        Path(env['HARNESS_OCR_META']).write_text(json.dumps({
            'ok': True, 'language': env['HARNESS_OCR_LANG'],
            'width': 800, 'height': 600,
            'region': env['HARNESS_OCR_REGION'] or None,
            'text': text,
            'lines': lines if lines is not None else [
                {'text': text, 'bbox': [10, 20, 100, 18], 'words': []}],
        }), encoding='utf-8')
        return SimpleNamespace(returncode=0, stdout=b'', stderr=b'')

    return runner


def test_capture_saves_png_and_returns_image_result(tmp_path):
    events = []

    result = tool_screen.capture(
        region='0,0,200,100', directory=tmp_path,
        runner=_capture_runner(), audit=lambda event, **data: events.append((event, data)))

    assert isinstance(result, tool_documents.ImageResult)
    assert '截图已保存：' in result.text
    assert '200x100' not in result.text  # 尺寸来自后端，不是请求区域
    assert '800x600px' in result.text
    attachment = result.images[0]
    saved = Path(attachment['path'])
    assert saved.is_file()
    assert saved.parent == tmp_path
    assert saved.read_bytes() == PNG
    assert attachment['type'] == 'harness_image'
    assert attachment['bytes'] == len(PNG)
    assert events[0][0] == 'screen_captured'
    assert events[0][1]['sha1'] == saved.stem
    assert events[0][1]['region'] == [0, 0, 200, 100]


def test_capture_returns_error_text_on_failure(tmp_path):
    def runner(script, env, timeout):
        raise OSError('powershell missing')

    result = tool_screen.capture(directory=tmp_path, runner=runner)

    assert result.startswith('截屏失败：')
    assert 'powershell missing' in result


def test_capture_reports_unsaveable_directory(tmp_path):
    blocker = tmp_path / 'not-a-dir'
    blocker.write_text('x', encoding='utf-8')

    result = tool_screen.capture(
        directory=blocker, runner=_capture_runner())

    assert result.startswith('截图存不下来')


def test_ocr_defaults_to_latest_screenshot(tmp_path):
    old = tmp_path / 'old.png'
    new = tmp_path / 'new.png'
    old.write_bytes(PNG)
    new.write_bytes(PNG)
    past = time.time() - 60
    os.utime(old, (past, past))
    events = []

    result = tool_screen.ocr(
        directory=tmp_path, runner=_ocr_runner('hi'),
        audit=lambda event, **data: events.append((event, data)))

    assert '识别结果（zh-Hans-CN）' in result
    assert 'hi' in result
    assert '没有留下坐标信息' in result
    assert '1. [10,20,100,18] hi' in result
    assert str(new) in result
    assert events[0][0] == 'screen_ocr'
    assert events[0][1]['chars'] == 2
    assert 'hi' not in events[0][1].values()


def test_ocr_without_screenshots_is_an_error(tmp_path):
    result = tool_screen.ocr(directory=tmp_path)

    assert result.startswith('识别失败：还没有截图')


def test_ocr_rejects_paths_outside_screenshot_dir(tmp_path):
    outside = tmp_path.parent / 'outside.png'
    outside.write_bytes(PNG)
    shots = tmp_path / 'shots'
    shots.mkdir()

    result = tool_screen.ocr(str(outside), directory=shots)

    assert 'OCR is limited to screenshots' in result
    assert str(outside) in result
    outside.unlink()


def test_ocr_accepts_relative_name_inside_dir(tmp_path):
    image = tmp_path / 'shot.png'
    image.write_bytes(PNG)

    result = tool_screen.ocr('shot.png', directory=tmp_path,
                             runner=_ocr_runner('relative'))

    assert 'relative' in result
    assert str(image) in result


def test_ocr_truncates_long_text_and_limits_lines(tmp_path):
    image = tmp_path / 'shot.png'
    image.write_bytes(PNG)
    lines = [{'text': f'line {i}', 'bbox': [0, i, 10, 5], 'words': []}
             for i in range(250)]

    result = tool_screen.ocr(
        'shot.png', directory=tmp_path,
        runner=_ocr_runner('x' * 20_000, lines))

    assert '原文 20000 字' in result
    assert '还有 50 行未显示' in result
    assert '250. ' not in result


def test_ocr_region_outside_image_is_reported(tmp_path):
    image = tmp_path / 'shot.png'
    image.write_bytes(PNG)

    result = tool_screen.ocr('shot.png', region='700,500,200,200',
                             directory=tmp_path, runner=_ocr_runner())

    assert result.startswith('识别失败：')
    assert '超出图片' in result
