"""API 访问日志轮转：配置解析与真实轮转行为。"""
import logging
import logging.handlers

import pytest

import harness_api


def test_defaults(monkeypatch):
    monkeypatch.delenv(harness_api.ACCESS_LOG_MAX_BYTES_ENV, raising=False)
    monkeypatch.delenv(harness_api.ACCESS_LOG_BACKUPS_ENV, raising=False)
    assert harness_api.access_log_max_bytes() == 10 * 1024 * 1024
    assert harness_api.access_log_backups() == 3


@pytest.mark.parametrize('raw,expected', [
    ('', 10 * 1024 * 1024),
    ('garbage', 10 * 1024 * 1024),
    ('0', 64 * 1024),                 # 太小会刷碎片，按最小值
    ('-5', 64 * 1024),
    ('1048576', 1048576),
])
def test_max_bytes_parsing(monkeypatch, raw, expected):
    monkeypatch.setenv(harness_api.ACCESS_LOG_MAX_BYTES_ENV, raw)
    assert harness_api.access_log_max_bytes() == expected


@pytest.mark.parametrize('raw,expected', [
    ('', 3), ('garbage', 3), ('0', 0), ('-1', 0), ('99', 20), ('2', 2),
])
def test_backups_parsing(monkeypatch, raw, expected):
    monkeypatch.setenv(harness_api.ACCESS_LOG_BACKUPS_ENV, raw)
    assert harness_api.access_log_backups() == expected


def test_rotating_handler_actually_rotates(tmp_path, monkeypatch):
    """构造一个真实 handler，验证超过上限后会轮转、且只保留 N 份。"""
    monkeypatch.setenv(harness_api.ACCESS_LOG_MAX_BYTES_ENV, str(64 * 1024))
    monkeypatch.setenv(harness_api.ACCESS_LOG_BACKUPS_ENV, '2')

    log_path = tmp_path / 'api-access.log'
    handler = logging.handlers.RotatingFileHandler(
        log_path, maxBytes=harness_api.access_log_max_bytes(),
        backupCount=harness_api.access_log_backups(), encoding='utf-8')
    logger = logging.getLogger('test-rotate')
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    try:
        line = 'x' * 200
        for _ in range(1000):        # ~200KB，远超 64KB 上限
            logger.info(line)
    finally:
        handler.close()
        logger.removeHandler(handler)

    files = sorted(path.name for path in tmp_path.iterdir())
    assert log_path.name in files
    assert len([name for name in files if '.log.' in name]) <= 2
    assert log_path.stat().st_size <= 64 * 1024


def test_app_uses_rotating_handler(tmp_path, monkeypatch):
    """create_app 装配的是 RotatingFileHandler，而不是无上限的 FileHandler。

    `create_app` 每次新建一个局部 Logger（不注册到 logging 全局），所以从
    logging.getLogger 取不到；这里直接拦截构造器记录参数。
    """
    import logging.handlers as handlers_module

    from fastapi.testclient import TestClient

    captured = {}
    original = handlers_module.RotatingFileHandler

    def spy(filename, *args, **kwargs):
        captured.update(filename=str(filename),
                        maxBytes=kwargs.get('maxBytes'),
                        backupCount=kwargs.get('backupCount'))
        return original(filename, *args, **kwargs)

    monkeypatch.setattr(handlers_module, 'RotatingFileHandler', spy)
    log_path = tmp_path / 'access.log'
    monkeypatch.setenv('HARNESS_API_LOG', str(log_path))

    app = harness_api.create_app(object(), api_token='t',
                                 task_root=tmp_path / 'tasks',
                                 workspace=tmp_path, rate_limit=10000)
    with TestClient(app):
        pass

    assert captured, 'API did not install a rotating access log'
    assert captured['maxBytes'] == harness_api.access_log_max_bytes()
    assert captured['backupCount'] == harness_api.access_log_backups()
