"""QueuedPrompt：输入线程、队列顺序与 Ctrl-C 的两种语义。"""
from pathlib import Path

import pytest

import cli_input
import turn_control


def _reader(monkeypatch, events):
    class FakeSession:
        def __init__(self, **kwargs):
            self.events = list(events)

        def prompt(self, message):
            if not self.events:
                raise EOFError
            event = self.events.pop(0)
            if event == 'EOF':
                raise EOFError
            if event == '^C':
                raise KeyboardInterrupt
            return event

    monkeypatch.setattr(cli_input, 'PromptSession', FakeSession)
    monkeypatch.setattr(cli_input, '_history_path', lambda: None)
    return cli_input.QueuedPrompt(lambda: Path(), lambda: [])


def test_queued_lines_are_returned_in_order(monkeypatch):
    reader = _reader(monkeypatch, ['one', 'two'])

    assert reader('> ') == 'one'
    assert reader('> ') == 'two'
    with pytest.raises(EOFError):
        reader('> ')


def test_ctrl_c_when_idle_raises_interrupt(monkeypatch):
    reader = _reader(monkeypatch, ['^C'])

    with pytest.raises(KeyboardInterrupt):
        reader('> ')


def test_ctrl_c_during_turn_sets_cancel_without_queuing(monkeypatch):
    turn_control.begin_turn()   # 线程一启动就消费事件，必须先进入回合状态
    reader = _reader(monkeypatch, ['^C', 'after'])
    try:
        # 取消事件被线程消费，消息队列里只剩 'after'。
        assert reader('> ') == 'after'
        assert turn_control.cancel_requested()
    finally:
        turn_control.end_turn()

    assert not turn_control.cancel_requested()
    assert not turn_control.active()
