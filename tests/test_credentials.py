"""凭据加密：DPAPI 往返、加载顺序、迁移脚本与失败降级。"""
from pathlib import Path

import pytest

import credentials

pytestmark = pytest.mark.skipif(not credentials.available(),
                                reason='DPAPI is only available on Windows')

SECRET = 'super-secret-value'


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(credentials, 'store_path', lambda: tmp_path / 'credentials.bin')
    return tmp_path / 'credentials.bin'


def test_roundtrip(store):
    path = credentials.save({'LLM_API_KEY': SECRET, 'IGNORED': 'x'})
    assert path.exists()
    loaded = credentials.load()
    assert loaded == {'LLM_API_KEY': SECRET}     # 只保留已知密钥


def test_ciphertext_does_not_contain_plaintext(store):
    path = credentials.save({'LLM_API_KEY': SECRET})
    raw = path.read_bytes()
    assert SECRET.encode() not in raw
    assert raw.startswith(b'HARNESS1')


def test_load_missing_file_is_empty(store):
    assert credentials.load() == {}


def test_corrupt_file_raises(store):
    store.write_bytes(b'HARNESS1garbage')
    with pytest.raises(credentials.CredentialError):
        credentials.load()


def test_unknown_format_raises(store):
    store.write_bytes(b'not-ours')
    with pytest.raises(credentials.CredentialError, match='format'):
        credentials.load()


def test_save_requires_known_keys(store):
    with pytest.raises(credentials.CredentialError):
        credentials.save({'UNRELATED': 'value'})


def test_apply_overrides_environment(store, monkeypatch):
    credentials.save({'LLM_API_KEY': 'from-store',
                      'HARNESS_API_TOKEN': 'token-store'})
    env = {'LLM_API_KEY': 'from-env'}
    applied = credentials.apply(environ=env)
    assert sorted(applied) == ['HARNESS_API_TOKEN', 'LLM_API_KEY']
    assert env['LLM_API_KEY'] == 'from-store'      # 密文优先
    assert env['HARNESS_API_TOKEN'] == 'token-store'


def test_apply_on_corrupt_file_keeps_env(store, capsys):
    store.write_bytes(b'HARNESS1garbage')
    env = {'LLM_API_KEY': 'from-env'}
    assert credentials.apply(environ=env) == []
    assert env['LLM_API_KEY'] == 'from-env'        # 降级到 .env
    assert '继续使用 .env' in capsys.readouterr().out


def test_status_and_describe(store):
    assert '未迁移' in credentials.describe()
    credentials.save({'LLM_API_KEY': SECRET})
    state = credentials.status()
    assert state['exists'] and state['keys'] == ['LLM_API_KEY']
    assert '加密文件' in credentials.describe()


def test_fingerprint_is_stable_and_short():
    values = {'LLM_API_KEY': 'a', 'HARNESS_API_TOKEN': 'b'}
    first = credentials.fingerprint(values)
    assert first == credentials.fingerprint(dict(values))
    assert len(first) == 8
    assert first != credentials.fingerprint({'LLM_API_KEY': 'a'})


# ---------- 迁移脚本 ----------

@pytest.fixture()
def script(tmp_path, store, monkeypatch):
    import importlib.util

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        'secure_credentials', root / 'scripts' / 'secure_credentials.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    env_file = tmp_path / '.env'
    monkeypatch.setattr(module, '_env_path', lambda: env_file)
    return module, env_file


def test_migrate_writes_ciphertext_and_keeps_env(script):
    module, env_file = script
    env_file.write_text('# comment\nLLM_API_KEY=abc\nOTHER=1\n', encoding='utf-8')

    assert module.main(['migrate']) == 0
    assert credentials.load() == {'LLM_API_KEY': 'abc'}
    text = env_file.read_text(encoding='utf-8')
    assert 'LLM_API_KEY=abc' in text          # 默认不删
    assert '# comment' in text


def test_migrate_delete_env_secrets_removes_only_those_lines(script):
    module, env_file = script
    env_file.write_text(
        '# 说明\nLLM_API_KEY=abc\nOTHER=1\nFEISHU_APP_SECRET=xyz\n',
        encoding='utf-8')

    assert module.main(['migrate', '--delete-env-secrets']) == 0
    text = env_file.read_text(encoding='utf-8')
    assert 'LLM_API_KEY' not in text
    assert 'FEISHU_APP_SECRET' not in text
    assert 'OTHER=1' in text and '# 说明' in text
    assert set(credentials.load()) == {'LLM_API_KEY', 'FEISHU_APP_SECRET'}


def test_migrate_with_nothing_to_do_fails_cleanly(script):
    module, env_file = script
    env_file.write_text('OTHER=1\n', encoding='utf-8')
    assert module.main(['migrate']) == 2
    assert not credentials.store_path().exists()


def test_migrate_merges_with_existing_store(script):
    module, env_file = script
    credentials.save({'FEISHU_APP_SECRET': 'old'})
    env_file.write_text('LLM_API_KEY=new\n', encoding='utf-8')

    assert module.main(['migrate']) == 0
    assert credentials.load() == {'LLM_API_KEY': 'new',
                                 'FEISHU_APP_SECRET': 'old'}


def test_clear_removes_store(script):
    module, env_file = script
    credentials.save({'LLM_API_KEY': 'abc'})
    assert module.main(['clear']) == 0
    assert not credentials.store_path().exists()
    assert module.main(['clear']) == 0        # 幂等


def test_status_reports_env_plaintext(script, capsys):
    module, env_file = script
    env_file.write_text('LLM_API_KEY=abc\n', encoding='utf-8')
    assert module.main(['status']) == 0
    output = capsys.readouterr().out
    assert 'LLM_API_KEY' in output
