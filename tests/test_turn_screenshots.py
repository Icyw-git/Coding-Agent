"""本回合截图元数据提取：文件名校验、字节偏移与去重上限。"""

from types import SimpleNamespace

import turn_screenshots


def _event(sha1, **extra):
    return {'type': 'screen_captured', 'sha1': sha1, **extra}


def test_valid_name_accepts_only_content_addressed_png():
    sha1 = 'a' * 40
    assert turn_screenshots.valid_name(f'{sha1}.png')
    assert not turn_screenshots.valid_name(f'{sha1}.PNG')
    assert not turn_screenshots.valid_name(f'{"A" * 40}.png')
    assert not turn_screenshots.valid_name('../x.png')
    assert not turn_screenshots.valid_name(f'{sha1}.png.exe')
    assert not turn_screenshots.valid_name('')


def test_offset_reads_store_file_size_and_degrades_to_zero(tmp_path):
    store = SimpleNamespace(path=tmp_path / 'session.jsonl')
    store.path.write_bytes(b'x' * 17)
    assert turn_screenshots.offset(store) == 17
    assert turn_screenshots.offset(SimpleNamespace(path=tmp_path / 'missing')) == 0
    assert turn_screenshots.offset(object()) == 0


def test_collect_filters_foreign_events_bad_hashes_and_duplicates():
    first, second = 'a' * 40, 'b' * 40
    events = [
        {'type': 'message', 'sha1': 'c' * 40},
        _event('not-a-sha'),
        _event(first, bytes=120, width=800, height=600),
        _event(first.upper(), bytes=999),   # 大写同一哈希：归一化后去重
        _event(second, bytes='bad', width=None, height=4),
    ]
    shots = turn_screenshots.collect(events)
    assert [shot['name'] for shot in shots] == [f'{first}.png', f'{second}.png']
    assert shots[0] == {'name': f'{first}.png', 'sha1': first,
                        'bytes': 120, 'width': 800, 'height': 600}
    assert shots[1]['bytes'] == 0 and shots[1]['width'] == 0
    assert shots[1]['height'] == 4


def test_collect_honours_limit_and_zero_limit():
    events = [_event(f'{index:040x}') for index in range(1, 7)]
    assert len(turn_screenshots.collect(events)) == 4
    assert len(turn_screenshots.collect(events, limit=2)) == 2
    assert turn_screenshots.collect(events, limit=0) == []


def test_collect_tail_uses_offset_and_survives_store_errors():
    seen = {}

    class Store:
        def tail_events(self, after):
            seen['after'] = after
            return [_event('d' * 40)], after + 10

    assert turn_screenshots.collect_tail(Store(), -5)[0]['sha1'] == 'd' * 40
    assert seen['after'] == 0

    class Broken:
        def tail_events(self, after):
            raise OSError('gone')

    assert turn_screenshots.collect_tail(Broken(), 0) == []
    assert turn_screenshots.collect_tail(None, 0) == []
