"""turn_progress：进度行格式与 ticker 的清除行为。"""
import io
import time

from turn_progress import ProgressTicker, TurnProgress, format_progress_line

CR = chr(13)
CLEAR = CR + chr(27) + '[K'


def test_format_progress_line_thinking_and_tool():
    thinking = format_progress_line({'phase': 'thinking', 'elapsed': 5,
                                     'elapsed_text': '00:05'})
    tool = format_progress_line({'phase': 'tool', 'tool': 'bash',
                                 'elapsed': 7, 'elapsed_text': '00:07'})

    assert '思考中' in thinking and '00:05' in thinking
    # 进度行用中文工具名，不暴露 desktop_snapshot 这类标识符
    assert '正在执行命令' in tool and '00:07' in tool


def test_ticker_writes_progress_and_clears_line():
    stream = io.StringIO()
    progress = TurnProgress()
    ticker = ProgressTicker(progress, stream=stream, interval=0.01).start()
    try:
        for _ in range(100):
            if '⏳' in stream.getvalue():
                break
            time.sleep(0.005)
    finally:
        ticker.stop()

    output = stream.getvalue()
    assert '⏳' in output
    assert output.endswith(CLEAR)   # 停止时清空整行，不留下残影


def test_ticker_stop_is_idempotent():
    stream = io.StringIO()
    ticker = ProgressTicker(TurnProgress(), stream=stream, interval=60)

    ticker.stop()
    ticker.stop()

    assert stream.getvalue() == CLEAR + CLEAR


def test_emit_line_clears_progress_line_atomically():
    stream = io.StringIO()
    ticker = ProgressTicker(TurnProgress(), stream=stream, interval=60)

    ticker.emit_line('  • bash  pytest -q  1.20s ✓')

    assert stream.getvalue() == CLEAR + '  • bash  pytest -q  1.20s ✓' + chr(10)
