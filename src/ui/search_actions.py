"""Current transcript search and navigation across paged session history."""
from __future__ import annotations

import threading
from dataclasses import dataclass

from ui.modals.search import TranscriptSearchModal


@dataclass(frozen=True)
class SearchHit:
    block_id: str
    history_index: int | None
    snippet: str


def _message_text(message: dict) -> str:
    return str(message.get('content') or '')


def _block_text(block) -> str:
    payload = block.payload
    if block.kind in ('user', 'assistant', 'notice'):
        return str(payload.get('text') or '')
    if block.kind == 'tool':
        return '\n'.join(str(payload.get(key) or '') for key in
                          ('name', 'args', 'output', 'diff'))
    if block.kind == 'diff':
        return '\n'.join(str(payload.get(key) or '') for key in ('path', 'text'))
    return ''


def _snippet(text: str, needle: str, limit: int = 120) -> str:
    folded = text.casefold()
    at = folded.find(needle)
    if at < 0:
        return ' '.join(text.split())[:limit]
    start = max(0, at - limit // 3)
    end = min(len(text), start + limit)
    preview = ' '.join(text[start:end].split())
    if start:
        preview = '…' + preview
    if end < len(text):
        preview += '…'
    return preview


class SearchActionsMixin:
    def action_open_transcript_search(self) -> None:
        self.open_transcript_search()

    def open_transcript_search(self) -> None:
        self._transcript_search_generation = getattr(
            self, '_transcript_search_generation', 0) + 1
        self._transcript_search_hits: list[SearchHit] = []
        self._transcript_search_index = -1
        self._transcript_search_timer = None
        self._transcript_search_modal = None
        self._transcript_search_query = getattr(
            self, '_transcript_search_query', '')
        modal = TranscriptSearchModal(
            self._transcript_search_query_changed,
            self._transcript_search_move,
            self._transcript_search_query)
        self._transcript_search_modal = modal
        self.push_screen(modal, self._transcript_search_closed)

    def _transcript_search_query_changed(self, query: str) -> None:
        query = query.strip()
        self._transcript_search_query = query
        self._transcript_search_generation += 1
        generation = self._transcript_search_generation
        timer = self._transcript_search_timer
        if timer is not None:
            timer.stop()
        self._transcript_search_timer = None
        self._transcript_search_hits = []
        self._transcript_search_index = -1
        if not query:
            self.view.set_search('', None)
            self._transcript_search_modal.set_results(0)
            return
        self.view.set_search(query, None)
        self._transcript_search_modal.set_results(0, message='Searching…')
        self._transcript_search_timer = self.set_timer(
            0.12, lambda: self._start_transcript_search(query, generation))

    def _start_transcript_search(self, query: str, generation: int) -> None:
        history = self._history_messages
        blocks = self.model.snapshot()
        thread = threading.Thread(
            target=self._scan_transcript_search,
            args=(query, generation, history, blocks), daemon=True)
        thread.start()

    def _scan_transcript_search(self, query: str, generation: int,
                                history: list, blocks: list) -> None:
        needle = query.casefold()
        hits = []
        seen = set()
        try:
            for index, message in enumerate(history):
                if message.get('role') not in ('user', 'assistant', 'tool'):
                    continue
                text = _message_text(message)
                if needle in text.casefold():
                    block_id = f'history:{index}'
                    seen.add(block_id)
                    hits.append(SearchHit(
                        block_id, index, _snippet(text, needle)))
            for block in blocks:
                if block.id in seen:
                    continue
                text = _block_text(block)
                if needle in text.casefold():
                    hits.append(SearchHit(
                        block.id, None, _snippet(text, needle)))
        except Exception as exc:
            hits = []
            error = str(exc)
        else:
            error = ''
        try:
            self.call_from_thread(self._transcript_search_ready,
                                  query, generation, history, hits, error)
        except Exception:
            pass

    def _transcript_search_ready(self, query: str, generation: int,
                                 history: list, hits: list[SearchHit],
                                 error: str) -> None:
        if (generation != self._transcript_search_generation
                or history is not self._history_messages
                or self._transcript_search_modal is None
                or self.screen is not self._transcript_search_modal):
            return
        modal = self._transcript_search_modal
        if error:
            self._transcript_search_hits = []
            self.view.set_search('', None)
            modal.set_results(0, message=f'Search failed: {error}')
            return
        self._transcript_search_hits = hits
        self._transcript_search_index = 0 if hits else -1
        if hits:
            self._focus_transcript_search_hit(0)
        else:
            self.view.set_search(query, None)
            modal.set_results(0)

    def _transcript_search_move(self, direction: int) -> None:
        hits = self._transcript_search_hits
        if not hits:
            return
        current = self._transcript_search_index
        index = (current + direction) % len(hits)
        self._focus_transcript_search_hit(index)

    def _focus_transcript_search_hit(self, index: int) -> None:
        hits = self._transcript_search_hits
        if not hits:
            return
        index %= len(hits)
        hit = hits[index]
        if hit.history_index is not None:
            while self._history_start > hit.history_index:
                if not self.load_older_history():
                    break
        self._flush_layout(force=True)
        start = self.doc_layout.block_start(hit.block_id)
        if start is None:
            self._transcript_search_modal.set_results(
                len(hits), index, hit.snippet,
                'Match is no longer available in this session')
            return
        self._transcript_search_index = index
        self.view.set_search(self._transcript_search_query, hit.block_id)
        viewport_height = max(1, self.view.size.height)
        self.view.scroll_to(y=max(0, start - viewport_height // 3),
                            animate=False)
        self.view.refresh()
        self._transcript_search_modal.set_results(
            len(hits), index, hit.snippet)

    def _transcript_search_closed(self, _result) -> None:
        self._transcript_search_generation += 1
        timer = self._transcript_search_timer
        if timer is not None:
            timer.stop()
        self._transcript_search_timer = None
        self._transcript_search_modal = None
        self._transcript_search_hits = []
        self._transcript_search_index = -1
        self.view.set_search('', None)
