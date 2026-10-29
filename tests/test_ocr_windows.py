"""ocr_windows：WinRT 脚本的 JSON 契约、语言缺失提示与区域校验。"""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import ocr_windows


def _ocr_runner(meta, *, returncode=0, stderr=b''):
    calls = []

    def runner(script, env, timeout):
        calls.append({'script': script, 'env': env, 'timeout': timeout})
        Path(env['HARNESS_OCR_META']).write_text(
            json.dumps(meta, ensure_ascii=False), encoding='utf-8')
        return SimpleNamespace(returncode=returncode, stdout=b'', stderr=stderr)

    return runner, calls


def _image(tmp_path, name='shot.png'):
    path = tmp_path / name
    path.write_bytes(b'\x89PNG\r\n\x1a\n' + b'body')
    return path


def test_recognize_returns_text_lines_and_passes_defaults(tmp_path):
    meta = {
        'ok': True,
        'language': 'zh-Hans-CN',
        'width': 800,
        'height': 600,
        'region': None,
        'text': '第一行\nsecond',
        'lines': [
            {'text': '第一行', 'bbox': [10, 20, 100, 20],
             'words': [{'text': '第一行', 'bbox': [10, 20, 100, 20]}]},
            {'text': 'second', 'bbox': [10, 50, 80, 18], 'words': []},
            'ignored',
        ],
    }
    runner, calls = _ocr_runner(meta)
    image = _image(tmp_path)

    result = ocr_windows.recognize(image, runner=runner)

    env = calls[0]['env']
    assert env['HARNESS_OCR_IN'] == str(image)
    assert env['HARNESS_OCR_LANG'] == 'zh-Hans-CN'
    assert env['HARNESS_OCR_REGION'] == ''
    assert result.text == '第一行\nsecond'
    assert [line['text'] for line in result.lines] == ['第一行', 'second']
    assert result.lines[0]['words'][0]['bbox'] == [10, 20, 100, 20]
    assert (result.width, result.height) == (800, 600)
    assert result.language == 'zh-Hans-CN'


def test_recognize_forwards_region_and_language(tmp_path):
    meta = {'ok': True, 'language': 'en-US', 'width': 800, 'height': 600,
            'region': '10,20,100,50', 'text': 'hi', 'lines': []}
    runner, calls = _ocr_runner(meta)

    result = ocr_windows.recognize(
        _image(tmp_path), lang='en-US', region='10,20,100,50', runner=runner)

    assert calls[0]['env']['HARNESS_OCR_LANG'] == 'en-US'
    assert calls[0]['env']['HARNESS_OCR_REGION'] == '10,20,100,50'
    assert result.region == (10, 20, 100, 50)
    assert result.text == 'hi'


def test_missing_language_pack_has_install_hint(tmp_path):
    runner, _ = _ocr_runner({
        'ok': False,
        'error': 'ocr_language_unavailable',
        'available': ['en-US'],
    }, returncode=1)

    with pytest.raises(ocr_windows.OcrError) as excinfo:
        ocr_windows.recognize(_image(tmp_path), lang='ja-JP', runner=runner)

    message = str(excinfo.value)
    assert "'ja-JP'" in message
    assert 'en-US' in message
    assert 'Get-WindowsCapability' in message


def test_ocr_failure_message_is_surfaced(tmp_path):
    runner, _ = _ocr_runner({
        'ok': False, 'error': 'ocr_failed',
        'message': 'region exceeds image',
    }, returncode=1)

    with pytest.raises(ocr_windows.OcrError, match='region exceeds image'):
        ocr_windows.recognize(_image(tmp_path), runner=runner)


def test_missing_metadata_reports_runner_failure(tmp_path):
    def runner(script, env, timeout):
        return SimpleNamespace(returncode=3, stdout=b'', stderr=b'no winrt')

    with pytest.raises(ocr_windows.OcrError, match='no winrt'):
        ocr_windows.recognize(_image(tmp_path), runner=runner)


def test_region_outside_image_is_rejected_after_metadata(tmp_path):
    meta = {'ok': True, 'language': 'zh-Hans-CN', 'width': 100, 'height': 100,
            'text': '', 'lines': []}
    runner, _ = _ocr_runner(meta)

    with pytest.raises(ocr_windows.OcrError, match='超出图片'):
        ocr_windows.recognize(
            _image(tmp_path), region='50,50,60,60', runner=runner)


def test_missing_image_fails_before_runner(tmp_path):
    with pytest.raises(ocr_windows.OcrError, match='找不到这张图片'):
        ocr_windows.recognize(tmp_path / 'missing.png')


def test_region_crop_uses_five_argument_software_bitmap_overload():
    """BitmapTransform 不能单独传给 GetSoftwareBitmapAsync（真机只接受五参数重载）。"""
    script = ocr_windows.OCR_SCRIPT
    assert 'GetSoftwareBitmapAsync($transform)' not in script
    assert '$decoder.GetSoftwareBitmapAsync(' in script
    for token in ('BitmapPixelFormat]::Bgra8', 'BitmapAlphaMode]::Premultiplied',
                  'ExifOrientationMode]::IgnoreExifOrientation',
                  'ColorManagementMode]::DoNotColorManage'):
        assert token in script
