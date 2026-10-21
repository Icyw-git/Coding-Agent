"""tool_files：注入的边界判定、checkpoint 顺序与编码保留。"""
from tool_files import edit_file, read_file, write_file


def _access(root, *, denied=None, log=None):
    log = [] if log is None else log

    def resolve(path, cwd):
        log.append(('resolve', path))
        return root / path

    def gate(tool, path, cwd):
        log.append((f'gate:{tool}', path))
        return denied

    def capture(path):
        log.append(('capture', path.name))

    return resolve, gate, capture


def test_read_uses_injected_resolve_and_gate(tmp_path):
    (tmp_path / 'a.txt').write_text('one\ntwo\n', encoding='utf-8')
    resolve, gate, _ = _access(tmp_path)

    assert read_file('a.txt', offset=1, resolve=resolve, gate=gate) == 'two'


def test_read_rejects_document_suffix_without_resolving(tmp_path):
    log = []
    resolve, gate, _ = _access(tmp_path, log=log)

    output = read_file('x.docx', resolve=resolve, gate=gate)

    assert 'read_document' in output
    assert log == [('gate:read', 'x.docx'), ('resolve', 'x.docx')]


def test_denied_write_skips_resolve_and_capture(tmp_path):
    log = []
    resolve, gate, capture = _access(tmp_path, denied='Error: denied', log=log)

    output = write_file('a.txt', 'x', resolve=resolve, gate=gate,
                        capture=capture)

    assert output == 'Error: denied'
    assert [entry[0] for entry in log] == ['gate:write']


def test_write_preserves_crlf_and_captures_before_write(tmp_path):
    target = tmp_path / 'a.txt'
    target.write_bytes(b'old\r\n')
    log = []
    resolve, gate, capture = _access(tmp_path, log=log)

    write_file('a.txt', 'new\n', resolve=resolve, gate=gate, capture=capture)

    assert target.read_bytes() == b'new\r\n'
    # 捕获发生在写盘之前，且路径已经解析成绝对路径。
    assert log[-1] == ('capture', 'a.txt')


def test_write_refuses_binary_overwrite(tmp_path):
    (tmp_path / 'b.bin').write_bytes(b'\x00\x01')
    resolve, gate, capture = _access(tmp_path)

    output = write_file('b.bin', 'text', resolve=resolve, gate=gate,
                        capture=capture)

    assert 'binary' in output


def test_edit_only_captures_after_match(tmp_path):
    target = tmp_path / 'a.txt'
    target.write_text('alpha\n', encoding='utf-8')
    log = []
    resolve, gate, capture = _access(tmp_path, log=log)

    missed = edit_file('a.txt', 'missing', 'x', resolve=resolve, gate=gate,
                       capture=capture)
    assert 'not found' in missed
    assert not any(entry[0] == 'capture' for entry in log)

    edited = edit_file('a.txt', 'alpha', 'beta', resolve=resolve, gate=gate,
                       capture=capture)
    assert edited == 'Edited a.txt'
    assert target.read_text(encoding='utf-8') == 'beta\n'
