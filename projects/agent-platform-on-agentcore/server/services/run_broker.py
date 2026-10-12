"""In-flight runs that outlive the HTTP response that started them.

Until this existed the SSE response iterated the turn's generator directly, so
whatever ended the response ended the turn: the generator closed, the runtime
stream behind it closed, and the runtime SDK cancelled the agent. Closing a tab
or clicking another thread discarded everything the agent had not yet said.

Here the generator is drained by a background task that runs to the end on its
own, and a response is one subscriber among any number. A subscriber that goes
away merely stops listening; the turn is still saved by the generator's own
`messageStop`/`end` handling. Cancelling the task is how a Stop click reaches the
generator now — its existing CancelledError path persists the partial turn and
writes `interrupted`.

A run has two phases. It is *reserved* first, before the pre-stream setup
(approval, target binding, thread creation — seconds, up to ~35s cold), so that
a second request on the thread is refused before it can write anything, and so
that a Stop pressed during setup has something to land on. It is *launched* once
the generator exists.

In-memory on purpose: the server runs one task (`desired_count=1`). More replicas
would need a shared event log instead of `ActiveRun.frames`.
"""
import asyncio
import logging
from typing import AsyncIterator, Dict, Iterable, List, Optional, Set

logger = logging.getLogger(__name__)

HEARTBEAT_FRAME = ": keep-alive\n\n"

# Frames a subscriber may fall behind by before it is dropped. A listener that
# has stopped reading (a tab in the background with a stalled connection) must
# not pin a copy of the whole run in its queue; the run itself is unaffected.
SUBSCRIBER_QUEUE_LIMIT = 10_000

# Marks the end of a run in a subscriber's queue. A sentinel rather than None so
# an (impossible, but cheap to rule out) empty frame cannot be mistaken for it.
_DONE = object()


class RunAlreadyActive(Exception):
    """The thread already has a run in flight; a second would interleave with it."""


class ActiveRun:
    def __init__(self, thread_id: str, owner_sub: Optional[str]):
        self.thread_id = thread_id
        # Who reserved it. A Stop on a reservation must come from them: the
        # cancel route lets a not-yet-created thread id through ownership (the
        # row does not exist during a first turn's setup), so this is the check.
        self.owner_sub = owner_sub
        # Every non-heartbeat frame so far, for replay to a late subscriber.
        self.frames: List[str] = []
        # Ids of the messages the thread held when this run began, plus this
        # run's own question. A client reattaching mid-run keeps exactly these
        # from the stored record and lets the replay rebuild the rest.
        self.baseline_message_ids: List[str] = []
        self.done = False
        self.task: Optional[asyncio.Task] = None
        # Stop arrived while reserved; applied at launch.
        self.cancel_requested = False
        # The loop the task lives on. `cancel` may be called from a worker
        # thread (sync routes run in FastAPI's threadpool) and a Task is not
        # thread-safe, so the cancel is handed to this loop.
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self._subscribers: Set[asyncio.Queue] = set()
        self._overflowed: Set[asyncio.Queue] = set()

    def publish(self, frame: str) -> None:
        # Append and enqueue with no await between: a subscriber registering at
        # any point sees each frame exactly once — in its snapshot or its queue.
        self.frames.append(frame)
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(frame)
            except asyncio.QueueFull:
                self._subscribers.discard(queue)
                self._overflowed.add(queue)
                logger.warning(
                    "Dropped a subscriber of thread %s: %d frames behind",
                    self.thread_id,
                    SUBSCRIBER_QUEUE_LIMIT,
                )

    def finish(self) -> None:
        self.done = True
        for queue in self._subscribers:
            queue.put_nowait(_DONE)

    async def subscribe(self, heartbeat_interval: float) -> AsyncIterator[str]:
        """Replay what has happened, then follow the run live until it ends.

        Heartbeats are the subscriber's own: the generator's are dropped before
        buffering (a reattach would otherwise replay minutes of `: keep-alive`),
        and a subscriber that joins during a silent tool run still needs bytes on
        the wire before the Next proxy's 30s idle cut.
        """
        queue: asyncio.Queue = asyncio.Queue(maxsize=SUBSCRIBER_QUEUE_LIMIT)
        self._subscribers.add(queue)
        # Taken right after registering, before any await, so the two agree.
        snapshot = list(self.frames)
        finished = self.done
        try:
            for frame in snapshot:
                yield frame
            if finished:
                return
            while True:
                if queue in self._overflowed:
                    # Fell too far behind and was cut off. Ending here rather
                    # than draining the backlog: the frames after the cut are
                    # gone, so a partial drain would only mislead.
                    return
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=heartbeat_interval)
                except asyncio.TimeoutError:
                    yield HEARTBEAT_FRAME
                    continue
                if item is _DONE:
                    return
                yield item
        finally:
            # A closed tab lands here as GeneratorExit. Only the listener leaves.
            self._subscribers.discard(queue)
            self._overflowed.discard(queue)


class RunBroker:
    def __init__(self) -> None:
        self._runs: Dict[str, ActiveRun] = {}

    def get(self, thread_id: str) -> Optional[ActiveRun]:
        return self._runs.get(thread_id)

    def reserve(self, thread_id: str, owner_sub: Optional[str] = None) -> ActiveRun:
        """Claim the thread for a run that is still being set up.

        Raises RunAlreadyActive if a run (reserved or launched) is already there.
        Must be followed by `launch` or `release`.
        """
        if thread_id in self._runs:
            raise RunAlreadyActive(thread_id)
        run = ActiveRun(thread_id, owner_sub)
        run.loop = asyncio.get_running_loop()
        self._runs[thread_id] = run
        return run

    def launch(
        self,
        run: ActiveRun,
        source: AsyncIterator[str],
        baseline_message_ids: Iterable[str] = (),
    ) -> ActiveRun:
        assert run.task is None, "launch needs a fresh reservation"
        # Re-read here: the task lives on whatever loop launches it, which is the
        # reserving loop in the server but need not be elsewhere.
        run.loop = asyncio.get_running_loop()
        run.baseline_message_ids = list(baseline_message_ids)
        run.task = asyncio.create_task(self._pump(run, source), name=f"run-{run.thread_id}")
        # Finishing lives on the task, not in `_pump`'s finally: a task cancelled
        # before its first step never enters the coroutine, so a finally there
        # never runs — and the thread would answer 409 until the process died.
        run.task.add_done_callback(lambda _task, run=run: self._finished(run))
        if run.cancel_requested:
            # A Stop arrived during setup. Scheduled rather than called: the
            # first step runs first, so the generator is inside its try when the
            # cancellation lands and its own handler saves the turn and writes
            # `interrupted`.
            run.loop.call_soon(run.task.cancel)
        return run

    def release(self, run: ActiveRun) -> None:
        """Give up a reservation whose setup failed; no run will follow."""
        assert run.task is None, "a launched run ends on its own"
        self._finished(run)

    def start(
        self, thread_id: str, source: AsyncIterator[str], owner_sub: Optional[str] = None
    ) -> ActiveRun:
        """reserve + launch for callers with nothing to set up in between."""
        return self.launch(self.reserve(thread_id, owner_sub), source)

    def _finished(self, run: ActiveRun) -> None:
        run.finish()
        if self._runs.get(run.thread_id) is run:
            del self._runs[run.thread_id]

    async def _pump(self, run: ActiveRun, source: AsyncIterator[str]) -> None:
        try:
            async for frame in source:
                if frame == HEARTBEAT_FRAME:
                    continue
                run.publish(frame)
        except asyncio.CancelledError:
            # Stop, or server shutdown. The generator has already written
            # `interrupted` on its way out; nothing to add.
            logger.info("Run on thread %s was cancelled", run.thread_id)
        except Exception:
            # The generator turns its own failures into `error` events, so this
            # is a bug in the plumbing, not a failed turn. Log it; the done
            # callback still ends the subscribers so nobody waits forever.
            logger.exception("Run on thread %s died outside the generator", run.thread_id)

    def cancel(self, thread_id: str, owner_sub: Optional[str] = None) -> bool:
        """Stop the run on `thread_id`; True if there was one to stop.

        A run still reserved (setup in progress) is stopped the moment it
        launches. Only its reserver may do that — see ActiveRun.owner_sub.
        """
        run = self._runs.get(thread_id)
        if run is None or run.done:
            return False
        if run.task is None:
            if run.owner_sub is not None and owner_sub is not None and run.owner_sub != owner_sub:
                return False
            run.cancel_requested = True
            return True
        self._request_cancel(run)
        return True

    @staticmethod
    def _request_cancel(run: ActiveRun) -> None:
        assert run.task is not None and run.loop is not None
        try:
            on_loop = asyncio.get_running_loop() is run.loop
        except RuntimeError:
            on_loop = False
        if on_loop:
            run.task.cancel()
        else:
            run.loop.call_soon_threadsafe(run.task.cancel)

    async def shutdown(self) -> None:
        """Cancel every run and wait: a redeploy must not leave threads `busy`."""
        runs = list(self._runs.values())
        tasks = []
        for run in runs:
            if run.task is None:
                # Still reserved: its launch, if it comes, must die at once.
                run.cancel_requested = True
            else:
                run.task.cancel()
                tasks.append(run.task)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
