"""Batch already-public text; never expose a private model JSON stream here.

Intended product path: apps/supportflow/jobs/public_text.py. Producers must own
their text's authorization/claim validation. This module supplies only bounded
aggregation and generation-fenced, replay-safe durable event writes.
"""
from dataclasses import dataclass
from time import monotonic

from . import core, event_hints
from .contracts import StopFragment


CHARACTERS = 256
WINDOW_MS = 100
MAX_TEXT = 12000


def milliseconds(): return monotonic() * 1000


def valid_text(value, *, maximum, empty=False):
    if not isinstance(value, str) or '\0' in value or len(value) > maximum or not empty and not value:
        raise ValueError('Invalid public text')
    value.encode('utf-8')
    return value


@dataclass(frozen=True)
class Flush:
    delta: str
    trigger: str
    first_pending_at_ms: float
    at_ms: float


class PublicTextBatcher:
    def __init__(self, emit, *, clock=milliseconds):
        self.emit, self.clock = emit, clock
        self.pending = ''
        self.first = None
        self.total = 0
        self.last_receipt = None
        self.completed = False

    def timeout_seconds(self):
        return None if self.first is None else max(0.0, (self.first + WINDOW_MS - self.clock()) / 1000)

    def _flush(self, trigger):
        if not self.pending:
            return
        value = Flush(self.pending, trigger, self.first, self.clock())
        self.pending, self.first = '', None
        self.emit(value)

    def flush_due(self):
        if self.first is not None and self.clock() >= self.first + WINDOW_MS:
            self._flush('deadline')

    def feed(self, text, *, received_at_ms=None):
        if self.completed:
            raise ValueError('Public stream is already complete')
        valid_text(text, maximum=MAX_TEXT, empty=True)
        if self.total + len(text) > MAX_TEXT:
            raise ValueError('Public text budget exceeded')
        received = self.clock() if received_at_ms is None else received_at_ms
        if (type(received) not in (int, float) or not 0 <= received < float('inf')
                or self.last_receipt is not None and received < self.last_receipt):
            raise ValueError('Invalid public text receipt clock')
        self.last_receipt = received
        if self.first is not None and received >= self.first + WINDOW_MS:
            self._flush('deadline')
        self.total += len(text)
        while text:
            if self.first is None:
                self.first = received
            room = CHARACTERS - len(self.pending)
            self.pending += text[:room]
            text = text[room:]
            if len(self.pending) == CHARACTERS:
                self._flush('characters')

    def complete(self):
        if not self.completed:
            self._flush('complete')
            self.completed = True


class PublicTextWriter:
    """Internal writer, not a model adapter or public HTTP capability.

    Existing business finalization already writes message.final. Such a caller
    uses delta() here and its existing finalizer, rather than calling final()
    twice. The standalone final() supports other trusted message producers and
    the labelled software protocol probe.
    """
    def __init__(self, conn, run_id, generation):
        self.conn, self.run_id, self.generation = conn, run_id, generation
        self.message_id = run_id + ':reply'
        self.gate = core.Gate(conn, run_id, generation)

    def _messages(self):
        return self.conn.execute('''SELECT seq,type,payload,execution_generation FROM run_events
            WHERE run_id=%s AND type IN ('message.delta','message.final')
              AND payload->>'message_id'=%s ORDER BY seq''', (self.run_id, self.message_id)).fetchall()

    def _append(self, run, kind, payload):
        updated = self.conn.execute('UPDATE runs SET last_event_seq=last_event_seq+1 WHERE run_id=%s RETURNING *',
                                    (self.run_id,)).fetchone()
        core.append_event(self.conn, updated, kind, {'message_id': self.message_id, **payload})
        return updated['last_event_seq']

    def delta(self, text, part_index):
        valid_text(text, maximum=CHARACTERS)
        if type(part_index) is not int or not 1 <= part_index <= MAX_TEXT:
            raise ValueError('Invalid public stream part')
        with event_hints.transaction(self.conn):
            run = self.gate._current()
            if not core.lock_final_scope(self.conn, run):
                raise StopFragment('AUTHORIZATION_REVOKED')
            messages = self._messages()
            deltas = [row for row in messages if row['type'] == 'message.delta']
            if part_index <= len(deltas):
                previous = deltas[part_index - 1]
                if previous['payload']['delta'] != text:
                    raise StopFragment('MESSAGE_IMMUTABLE')
                return previous['seq']
            if any(row['type'] == 'message.final' for row in messages):
                raise StopFragment('MESSAGE_FINALIZED')
            if part_index != len(deltas) + 1 or sum(len(row['payload']['delta']) for row in deltas) + len(text) > MAX_TEXT:
                raise StopFragment('MESSAGE_PART_INVALID')
            return self._append(run, 'message.delta', {'delta': text})

    def final(self, text):
        valid_text(text, maximum=MAX_TEXT, empty=True)
        with event_hints.transaction(self.conn):
            run = self.gate._current()
            if not core.lock_final_scope(self.conn, run):
                raise StopFragment('AUTHORIZATION_REVOKED')
            messages = self._messages()
            previous = next((row for row in messages if row['type'] == 'message.final'), None)
            if previous is not None:
                if previous['payload']['text'] != text:
                    raise StopFragment('MESSAGE_IMMUTABLE')
                return previous['seq']
            return self._append(run, 'message.final', {'text': text})
