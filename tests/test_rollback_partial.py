"""回滚部分失败时不能只报「已恢复 N 个」。

`rollback()` 恢复时 `except OSError: continue`——这在 Windows 上很常见：文件被别人
（编辑器、另一个进程）打开着，`shutil.copy2` 就是 PermissionError。

结果是仓库停在**半新半旧**的状态，而界面只说「已恢复 2 个文件：a、b」。这句话本身
没错，但它和「这次只需要恢复 2 个」长得一样——用户无法分辨自己要不要手工补那个文件。

本该恢复的数量在快照记录里（`snapshotted`）已经存着，说出来是零成本的。
"""

import self_improve_ledger as ledger


def test_partial_rollback_is_reported(tmp_path, monkeypatch):
    source = tmp_path / 'a.py'
    source.write_text('v1', encoding='utf-8')
    other = tmp_path / 'b.py'
    other.write_text('v1', encoding='utf-8')

    change_id = ledger.record(tmp_path, paths=['a.py', 'b.py'],
                              summary='改两个文件', workdir=tmp_path)

    import shutil
    real_copy = shutil.copy2

    def flaky(src, dst, *a, **k):
        if str(dst).endswith('b.py'):
            raise PermissionError(13, '被占用', str(dst))
        return real_copy(src, dst, *a, **k)

    monkeypatch.setattr(shutil, 'copy2', flaky)

    restored = ledger.rollback(tmp_path, change_id)
    assert restored == ['a.py']          # 只恢复了一个

    # 界面要能说出「本该恢复 2 个」
    assert ledger.snapshotted_paths(tmp_path, change_id) == ['a.py', 'b.py']


def test_ui_message_names_the_files_that_failed(tmp_path, monkeypatch):
    """界面上要能读出「有 1 个没恢复」——不是只有一句「已恢复 1 个」。"""
    (tmp_path / 'a.py').write_text('v1', encoding='utf-8')
    (tmp_path / 'b.py').write_text('v1', encoding='utf-8')
    change_id = ledger.record(tmp_path, paths=['a.py', 'b.py'], summary='改两个',
                              workdir=tmp_path)

    import shutil
    real_copy = shutil.copy2
    monkeypatch.setattr(shutil, 'copy2', lambda src, dst, *a, **k: (
        (_ for _ in ()).throw(PermissionError(13, '占用', str(dst)))
        if str(dst).endswith('b.py') else real_copy(src, dst, *a, **k)))

    restored = ledger.rollback(tmp_path, change_id)
    expected = ledger.snapshotted_paths(tmp_path, change_id)
    missed = [name for name in expected if name not in restored]
    assert missed == ['b.py']
