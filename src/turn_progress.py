"""Read live tool progress from the existing session event stream."""

import time


def format_duration(seconds: float) -> str:
    """Compact elapsed time for progress and completion messages."""
    total = max(0, int(seconds))
    minutes, seconds = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    return f'{hours:02d}:{minutes:02d}:{seconds:02d}' if hours else f'{minutes:02d}:{seconds:02d}'


class TurnProgress:
    def __init__(self):
        self.started = time.monotonic()
        self.store = None
        self.offset = 0
        self.tool = None
        self.tool_started = None

    def follow(self, store, offset):
        self.store, self.offset = store, offset

    def snapshot(self):
        if self.store is not None:
            events, self.offset = self.store.tail_events(self.offset)
            for event in events:
                if event.get('type') == 'tool_started':
                    self.tool = event.get('name')
                    self.tool_started = event.get('ts')
                elif event.get('type') == 'message' and event.get('message', {}).get('role') == 'tool':
                    self.tool = self.tool_started = None
        elapsed = max(0, time.monotonic() - self.started)
        return {
            'phase': 'tool' if self.tool else 'thinking',
            'tool': self.tool,
            'elapsed': int(elapsed),
            'elapsed_text': format_duration(elapsed),
            # Keep tool timing internal for diagnostics; clients only need the
            # overall turn clock and phase.
            'tool_elapsed': max(0, int(time.time() - self.tool_started)) if self.tool_started else 0,
        }
