"""Chat runs across server tasks.

A run lives in the memory of the task that started it (services/run_broker.py):
its generator, its replay buffer, its subscribers. With one task that was the
whole story. With two behind Cloud Map DNS, each request lands on either, and
the load test (bst, 2026-10-18, 12 turns) measured what that did: a second POST
on a busy thread started a second run 4 times in 12, a reattach found the run
3 times, a Stop reached it 7 times.

So the parts of a run another task must act on are shared, and nothing else:

- *Claim.* Before setup, the starting task writes a lease row conditionally
  (repositories/run_repository.py). A second request, on any task, fails the
  condition and gets the 409 a second tab always got.
- *Holder.* The row names the task (`ip:port`). A reattach or Stop that lands
  elsewhere is sent to that task over the VPC, where the run is local — the
  replay buffer never has to leave the task that fills it.
- *Liveness.* The holder renews every RUN_HEARTBEAT_SECONDS. A task killed
  without its shutdown leaves a row that stops being renewed and is free to
  claim RUN_LEASE_SECONDS later; there is no sweep to run.
- *Fallback Stop.* If the holder cannot be reached, the row is flagged and the
  holder sees the flag in its next renewal's response.

Without RUNS_TABLE (local development, one process) none of this runs and the
broker behaves as it always did.
"""
import logging
import os
import socket
import threading
import time
from typing import Any, AsyncIterator, Callable, Dict, Optional

import httpx

logger = logging.getLogger(__name__)

# Marks a request one task sent to another. The receiving task answers from its
# own memory and never forwards again, so two tasks that disagree about a lease
# cannot bounce a request between them.
FORWARDED_HEADER = "x-run-forwarded"


def task_address(port: int) -> str:
    """This task's VPC address, as other tasks reach it.

    The ECS task metadata endpoint gives the awsvpc ENI address; the hostname
    lookup is the fallback (it resolves to the same address on Fargate, and to
    something local everywhere else, which is fine where there is one process).
    """
    uri = os.getenv("ECS_CONTAINER_METADATA_URI_V4")
    if uri:
        try:
            task = httpx.get(f"{uri}/task", timeout=2.0).json()
            for container in task.get("Containers") or []:
                for network in container.get("Networks") or []:
                    for ip in network.get("IPv4Addresses") or []:
                        return f"{ip}:{port}"
        except Exception:
            logger.warning("ECS task metadata unreadable; using the hostname address", exc_info=True)
    try:
        ip = socket.gethostbyname(socket.gethostname())
    except OSError:
        ip = "127.0.0.1"
    return f"{ip}:{port}"


class RunLeases:
    """The lease rows, from this task's point of view."""

    def __init__(
        self,
        repository: Any,
        port: int,
        lease_seconds: float = 60,
        clock: Callable[[], float] = time.time,
        address: Callable[[int], str] = task_address,
    ):
        self.repository = repository
        self.lease_ms = int(lease_seconds * 1000)
        self._port = port
        self._clock = clock
        self._address = address
        self._holder: Optional[str] = None
        self._holder_lock = threading.Lock()

    @property
    def holder(self) -> str:
        # Resolved on first use, not at construction: building the service
        # happens at import, which must do no I/O.
        with self._holder_lock:
            if self._holder is None:
                self._holder = self._address(self._port)
                logger.info("Run leases held as %s", self._holder)
            return self._holder

    def _now_ms(self) -> int:
        return int(self._clock() * 1000)

    def acquire(self, thread_id: str, run_id: str, owner_sub: Optional[str]) -> bool:
        return self.repository.acquire(thread_id, run_id, self.holder, owner_sub, self._now_ms(), self.lease_ms)

    def renew(self, thread_id: str, run_id: str) -> Optional[Dict[str, Any]]:
        return self.repository.renew(thread_id, run_id, self._now_ms(), self.lease_ms)

    def release(self, thread_id: str, run_id: str) -> None:
        self.repository.release(thread_id, run_id)

    def remote_holder(self, thread_id: str) -> Optional[str]:
        """The other task running a turn on this thread, if one is."""
        row = self.repository.get(thread_id)
        if not row or int(row.get("lease_until") or 0) < self._now_ms():
            return None
        holder = str(row.get("holder") or "")
        return holder if holder and holder != self.holder else None

    def request_cancel(self, thread_id: str, owner_sub: Optional[str]) -> bool:
        return self.repository.request_cancel(thread_id, owner_sub, self._now_ms())


def _peer_headers(authorization: Optional[str]) -> Dict[str, str]:
    headers = {FORWARDED_HEADER: "1"}
    if authorization:
        # The holder authenticates the caller itself, exactly as if the request
        # had come from the web proxy.
        headers["authorization"] = authorization
    return headers


def forward_cancel(holder: str, path: str, authorization: Optional[str]) -> Optional[bool]:
    """POST the Stop to the holder; its `cancelled`, or None if unreachable."""
    try:
        resp = httpx.post(f"http://{holder}{path}", headers=_peer_headers(authorization), timeout=5.0)
        resp.raise_for_status()
        return bool(resp.json().get("cancelled"))
    except Exception:
        logger.warning("Could not hand a Stop to %s", holder, exc_info=True)
        return None


async def open_peer_stream(holder: str, path: str, authorization: Optional[str]) -> Optional[AsyncIterator[bytes]]:
    """Open the holder's attach stream; None if it has no such run or is unreachable.

    Opened before the response starts so a holder's 404 is still our 404. The
    read timeout is off: the holder sends heartbeats, and a turn may run for
    minutes.
    """
    client = httpx.AsyncClient(timeout=httpx.Timeout(10.0, read=None))
    try:
        resp = await client.send(
            client.build_request("GET", f"http://{holder}{path}", headers=_peer_headers(authorization)),
            stream=True,
        )
    except Exception:
        logger.warning("Could not reach %s to reattach", holder, exc_info=True)
        await client.aclose()
        return None
    if resp.status_code != 200:
        await resp.aclose()
        await client.aclose()
        return None

    async def relay() -> AsyncIterator[bytes]:
        try:
            async for chunk in resp.aiter_raw():
                yield chunk
        finally:
            await resp.aclose()
            await client.aclose()

    return relay()
