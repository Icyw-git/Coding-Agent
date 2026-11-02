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
    # 尺寸来自后端，不是请求区域（原来断言的是「200x100 不出现」，区域改成
    # 「0,0 200x100」之后会把请求区域也匹配进去，这里改成只校验尺寸那一行）
    assert '尺寸 800x600px' in result.text
    assert '尺寸 200x100' not in result.text
    assert '区域 0,0 200x100' in result.text
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

    assert '只能识别 screen_capture 存下来的截图' in result
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


# ---------- B4：OCR 把中文拆成带空格的字 ----------

def _ocr_runner_with(lines):
    """构造一个返回给定文本行的 OCR 假件。"""
    def runner(script, env, timeout):
        meta = {'ok': True, 'language': env.get('HARNESS_OCR_LANG') or 'zh-Hans-CN',
                'width': 800, 'height': 600, 'region': None, 'text': '',
                'lines': lines}
        out = Path(env['HARNESS_OCR_META'])
        out.write_text(json.dumps(meta, ensure_ascii=False), encoding='utf-8')
        return SimpleNamespace(returncode=0, stdout=b'', stderr=b'')
    return runner


def test_spaced_chinese_says_how_to_read_it(tmp_path):
    """引擎会把「记住我」识别成「记 住 我」——按控件名搜索必然搜不到。

    只在真的出现拆字时才说这一句：每个 OCR 调用都付这句话的 token 不值。
    """
    # 需要一个存在的截图文件：ocr() 先解析路径再调 runner
    (tmp_path / 'shot.png').write_bytes(PNG)
    lines = [
        {'text': '记 住 我', 'bbox': [10, 20, 60, 18], 'words': []},
        {'text': '高 级 选 项', 'bbox': [10, 60, 90, 18], 'words': []},
    ]
    result = tool_screen.ocr('shot.png', directory=tmp_path,
                             runner=_ocr_runner_with(lines))
    assert '带空格' in result
    result.encode('gbk')


def test_normal_chinese_gets_no_hint(tmp_path):
    """没拆字就不要多一句话。"""
    (tmp_path / 'shot.png').write_bytes(PNG)
    lines = [{'text': '记住我', 'bbox': [10, 20, 60, 18], 'words': []},
             {'text': '高级选项', 'bbox': [10, 60, 90, 18], 'words': []}]
    result = tool_screen.ocr('shot.png', directory=tmp_path,
                             runner=_ocr_runner_with(lines))
    assert '带空格' not in result


def test_english_only_gets_no_hint(tmp_path):
    (tmp_path / 'shot.png').write_bytes(PNG)
    lines = [{'text': 'Save changes', 'bbox': [10, 20, 120, 18], 'words': []},
             {'text': 'Cancel', 'bbox': [10, 60, 80, 18], 'words': []}]
    result = tool_screen.ocr('shot.png', directory=tmp_path,
                             runner=_ocr_runner_with(lines))
    assert '带空格' not in result


def test_a_single_spaced_line_is_not_enough(tmp_path):
    """一行里带空格可能是排版，不足以断言"引擎在拆字"。"""
    (tmp_path / 'shot.png').write_bytes(PNG)
    lines = [{'text': '记 住 我', 'bbox': [10, 20, 60, 18], 'words': []},
             {'text': 'Save', 'bbox': [10, 60, 80, 18], 'words': []}]
    result = tool_screen.ocr('shot.png', directory=tmp_path,
                             runner=_ocr_runner_with(lines))
    assert '带空格' not in result
