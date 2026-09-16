"""The consumer-facing `Stream` object and its ring buffer (Req 5.1-5.5,
5.7-5.10, 5.12, 6.9).

Design summary (see design.md, "Streaming" and "Ring buffer and
backpressure"):

- A fixed-capacity ring buffer sits between the producer (a background
  thread pulling from the adapter's `StreamSource.next_block()`, which
  blocks) and the consumer (`Stream.__next__`). `collections.deque(maxlen=)`
  is deliberately not used: its silent auto-eviction gives no hook to
  count discards or mark the next block `degraded`.
- `seq` is assigned by the producer at production time, before the block
  ever touches the ring buffer, so a discard leaves a gap in `seq` rather
  than a renumbering.
- Release is via `weakref.finalize` against a `_StreamResources` holder
  that has no back-reference to `Stream` (a back-reference would keep the
  `Stream` reachable forever from the finalizer's registration, and the
  finalizer would never fire). `atexit` registers the same release logic
  as a documented, best-effort backstop; `__del__` is not used anywhere
  (see design.md's rationale: reference cycles and shutdown ordering
  defeat `__del__`).
- Emptiness policy while the buffer is empty and the stream is not closed:
  design.md's example loop (`for reading in s: ...`) implies `__next__`
  yields the next block once available. That means an empty buffer must
  block the consumer rather than raise or return early -- otherwise a
  `for` loop over a live stream would spin/exit before the stream is
  closed. This is a reasonable-call point the design text does not pin
  down explicitly beyond the backpressure/discard direction (Req 5.8
  only speaks to the producer being *faster* than the consumer); the
  choice made here is: block on empty until a block arrives or the
  stream closes, at which point `__next__` raises `StopIteration`.
"""

from __future__ import annotations

import atexit
import threading
import weakref
from collections import deque
from typing import TYPE_CHECKING

from sensortap.registry.errors import ClosedStreamError
from sensortap.schema.enums import Status
from sensortap.schema.reading import Reading

if TYPE_CHECKING:
    from sensortap.adapters.protocol import StreamSource

#: Buffer depth bounds (Req 5.10).
_MIN_BUFFER_BLOCKS = 2
_MAX_BUFFER_BLOCKS = 1024

#: Window size for achieved_rate_hz() (Req 6.9).
_RATE_WINDOW = 100


class _RingBuffer:
    """An explicit fixed-capacity ring buffer of blocks.

    NOT `collections.deque(maxlen=...)` -- see module docstring. On push
    when full, the oldest block is evicted, `discarded` is incremented,
    and a one-shot `_degrade_next` flag is set so the next block actually
    delivered to the consumer carries `status = degraded` (Req 5.8).
    """

    def __init__(self, capacity: int) -> None:
        self._capacity = capacity
        self._buf: list[Reading | None] = [None] * capacity
        self._head = 0  # index of the oldest occupied slot
        self._tail = 0  # index of the next slot to write
        self._count = 0
        self._discarded = 0
        self._degrade_next = False
        self._lock = threading.Lock()
        self._not_empty = threading.Condition(self._lock)
        self._closed = False

    def push(self, block: Reading) -> None:
        with self._lock:
            if self._closed:
                return
            if self._count == self._capacity:
                # Drop the oldest undelivered block (Req 5.8).
                self._head = (self._head + 1) % self._capacity
                self._discarded += 1
                self._degrade_next = True
            else:
                self._count += 1
            self._buf[self._tail] = block
            self._tail = (self._tail + 1) % self._capacity
            self._not_empty.notify()

    def pop(self) -> Reading | None:
        """Block until a block is available, the buffer is closed, or
        this call is woken with nothing to deliver on close.

        Returns `None` when the buffer has been closed and drained.
        """
        with self._lock:
            while self._count == 0 and not self._closed:
                self._not_empty.wait()
            if self._count == 0:
                return None
            block = self._buf[self._head]
            self._buf[self._head] = None
            self._head = (self._head + 1) % self._capacity
            self._count -= 1

            degrade = self._degrade_next
            self._degrade_next = False

        assert block is not None
        if degrade and block.status == Status.OK:
            block = Reading(
                id=block.id,
                t_mono=block.t_mono,
                t_wall=block.t_wall,
                values=block.values,
                seq=block.seq,
                status=Status.DEGRADED,
            )
        return block

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._not_empty.notify_all()

    @property
    def discarded(self) -> int:
        with self._lock:
            return self._discarded


class _StreamResources:
    """Holds only what is needed to release backend resources.

    Deliberately holds NO reference back to the owning `Stream` -- a
    back-reference would keep the resources object (and, transitively,
    anything it points to) reachable from the `weakref.finalize`
    registration for as long as the `Stream` exists, but more importantly
    it would create the appearance of a cycle that defeats the purpose of
    finalizing on unreachability. This object's only job is to be
    releasable once, idempotently, whether triggered by `Stream.close()`,
    GC finalization, or the `atexit` backstop.
    """

    __slots__ = ("_source", "_stop_event", "_producer_thread", "_lock", "_released")

    def __init__(
        self,
        source: "StreamSource",
        stop_event: threading.Event,
        producer_thread: threading.Thread | None,
    ) -> None:
        self._source = source
        self._stop_event = stop_event
        self._producer_thread = producer_thread
        self._lock = threading.Lock()
        self._released = False

    def release(self) -> None:
        with self._lock:
            if self._released:
                return
            self._released = True
        self._stop_event.set()
        if self._producer_thread is not None and self._producer_thread.is_alive():
            self._producer_thread.join(timeout=1.0)
        try:
            self._source.close()
        except Exception:
            # Release must not raise from a finalizer or atexit context;
            # best-effort only.
            pass


def _produce_loop(
    source: "StreamSource",
    ring: _RingBuffer,
    stop_event: threading.Event,
    seq_state: dict,
) -> None:
    """The producer thread body.

    Deliberately a free function taking only the pieces it needs (the
    `StreamSource`, the ring buffer, the stop event, and a small mutable
    `seq_state` dict) rather than a bound method on `Stream`. A bound
    method would capture `self`, and since this function runs on a
    thread referenced from `_StreamResources`, that would create a path
    back to the `Stream` object from its own resources holder -- exactly
    the back-reference `_StreamResources` is designed to avoid, and it
    would prevent `weakref.finalize` from ever firing.
    """
    while not stop_event.is_set():
        try:
            block = source.next_block()
        except Exception:
            # The adapter/backend raised (e.g. because it was closed out
            # from under the producer). Stop producing; the consumer
            # will observe a drained, closed buffer.
            break
        if stop_event.is_set():
            break

        seq = seq_state["next_seq"]
        seq_state["next_seq"] = seq + 1
        if block.seq != seq:
            block = Reading(
                id=block.id,
                t_mono=block.t_mono,
                t_wall=block.t_wall,
                values=block.values,
                seq=seq,
                status=block.status,
            )
        ring.push(block)
    ring.close()


def _release_resources(resources: _StreamResources) -> None:
    """Module-level function used as the `weakref.finalize` callback and
    the `atexit` backstop. Takes the resources object as its sole
    argument (not a closure over `self`), matching `weakref.finalize`'s
    calling convention and avoiding any accidental capture of `Stream`.
    """
    resources.release()


class Stream:
    """Consumer-facing streaming object (Req 5.1).

    Usable both as an iterator and as a context manager. Wraps one
    adapter-side `StreamSource` (from `Adapter.open_stream()`) with a
    fixed-capacity ring buffer that decouples backend production rate
    from consumer consumption rate.

    A background producer thread continuously pulls blocks from the
    `StreamSource` (whose `next_block()` blocks until a block is ready)
    and pushes them into the ring buffer, assigning `seq` at production
    time -- before the buffer -- so that a discard leaves a gap in `seq`
    rather than renumbering (Req 5.4, 5.8). The consumer-facing
    `__next__` pulls from the ring buffer, blocking while it is empty.
    """

    def __init__(
        self,
        source: "StreamSource",
        *,
        sensor_id: str,
        buffer_blocks: int = 64,
        applied_rate_hz: float | None = None,
    ) -> None:
        if not isinstance(buffer_blocks, int) or isinstance(buffer_blocks, bool):
            raise ValueError(
                f"buffer_blocks must be an int in [{_MIN_BUFFER_BLOCKS}, "
                f"{_MAX_BUFFER_BLOCKS}], got {buffer_blocks!r}"
            )
        if not (_MIN_BUFFER_BLOCKS <= buffer_blocks <= _MAX_BUFFER_BLOCKS):
            raise ValueError(
                f"buffer_blocks must be in [{_MIN_BUFFER_BLOCKS}, "
                f"{_MAX_BUFFER_BLOCKS}], got {buffer_blocks!r}"
            )

        self._sensor_id = sensor_id
        self._buffer_blocks = buffer_blocks
        self._applied_rate_hz = applied_rate_hz
        self._source = source

        self._ring = _RingBuffer(buffer_blocks)

        self._closed_lock = threading.Lock()
        self._closed = False

        # Rate tracking over the most recent _RATE_WINDOW *delivered*
        # (consumer-side) samples (Req 6.9).
        self._delivered_lock = threading.Lock()
        self._delivered_t_mono: deque[float] = deque(maxlen=_RATE_WINDOW)

        # seq assignment happens at production time, in the producer
        # thread, starting at 0 for the first block of the stream
        # (Req 5.4). Held in a plain dict (not an attribute referenced by
        # the thread target) so the producer thread needs no reference to
        # `self` -- see `_produce_loop`'s docstring.
        seq_state = {"next_seq": 0}

        self._stop_event = threading.Event()
        self._producer_thread = threading.Thread(
            target=_produce_loop,
            args=(source, self._ring, self._stop_event, seq_state),
            name=f"sensortap-stream-{sensor_id}",
            daemon=True,
        )

        self._resources = _StreamResources(source, self._stop_event, self._producer_thread)
        # No back-reference from `self._resources` to `self` (see
        # `_StreamResources` docstring). Register the finalizer against
        # this Stream instance so it fires when the Stream becomes
        # unreachable without an explicit close() (Req 5.12).
        self._finalizer = weakref.finalize(self, _release_resources, self._resources)
        atexit.register(_release_resources, self._resources)

        self._producer_thread.start()

    # ------------------------------------------------------------------
    # public properties (Req 5.10, 6.9)
    # ------------------------------------------------------------------

    @property
    def sensor_id(self) -> str:
        return self._sensor_id

    @property
    def buffer_blocks(self) -> int:
        return self._buffer_blocks

    @property
    def discarded_blocks(self) -> int:
        return self._ring.discarded

    @property
    def applied_rate_hz(self) -> float | None:
        return self._applied_rate_hz

    def achieved_rate_hz(self) -> float | None:
        """Rate computed over the most recent 100 delivered blocks (or
        fewer if fewer have been delivered). `None` below 2 delivered
        blocks (Req 6.9).
        """
        with self._delivered_lock:
            samples = list(self._delivered_t_mono)
        if len(samples) < 2:
            return None
        span = samples[-1] - samples[0]
        if span <= 0:
            return None
        return (len(samples) - 1) / span

    # ------------------------------------------------------------------
    # close / iteration / context manager
    # ------------------------------------------------------------------

    def close(self) -> None:
        with self._closed_lock:
            if self._closed:
                return
            self._closed = True
        self._resources.release()
        # The finalizer is no longer needed once resources are released
        # explicitly; detach it so it does not run again redundantly at
        # GC/interpreter-shutdown time.
        self._finalizer.detach()
        try:
            atexit.unregister(_release_resources)
        except Exception:
            pass

    def __iter__(self) -> "Stream":
        return self

    def __next__(self) -> Reading:
        with self._closed_lock:
            if self._closed:
                raise ClosedStreamError(sensor_id=self._sensor_id)

        block = self._ring.pop()
        if block is None:
            # Buffer closed and drained: producer stopped (backend
            # exhausted/errored) without an explicit close() call.
            with self._closed_lock:
                if not self._closed:
                    self._closed = True
            raise StopIteration

        with self._delivered_lock:
            self._delivered_t_mono.append(block.t_mono)

        return block

    def __enter__(self) -> "Stream":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
