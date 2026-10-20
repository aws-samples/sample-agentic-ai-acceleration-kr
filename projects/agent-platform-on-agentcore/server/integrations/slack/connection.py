"""Socket Mode: one outbound WebSocket per integration, and the manager of them all.

A connection asks `apps.connections.open` for a single-use URL, connects, and
acknowledges every envelope the moment it arrives — Slack redelivers anything
not acked within a few seconds, and a turn takes far longer. The event itself is
handed to the DM handler as a separate task.

Slack may close a connection at any time (`disconnect`: `warning`,
`refresh_requested`, `link_disabled`); the connection simply opens a new one.
Transient failures back off up to a minute. A token Slack calls dead (revoked,
uninstalled app, missing scope) stops the connection and shows as `auth_error`:
retrying a revoked token forever only fills the log.

The manager connects every stored integration at startup, applies the admin
API's changes immediately, and re-reads the table every minute so a change made
by another process (create, delete) is picked up here too. One connection's
failure never touches another's.
"""
import asyncio
import json
import logging
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set, Tuple

from integrations.slack.api import SlackApi, SlackApiError
from integrations.slack.repository import STATUS_ACTIVE, SlackRepository

logger = logging.getLogger(__name__)

RECONCILE_SECONDS = 60
MAX_BACKOFF_SECONDS = 60
MAX_CONCURRENT_TURNS = 16
# How long a removed integration's (or a stopping server's) turns get to post
# their "interrupted" note and release their locks.
DRAIN_SECONDS = 10

Dispatch = Callable[[Dict[str, Any], str, Dict[str, Any]], None]


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class SocketModeConnection:
    def __init__(
        self,
        integration: Dict[str, Any],
        bot_token: str,
        app_token: str,
        api: SlackApi,
        dispatch: Dispatch,
        connect: Optional[Callable[..., Any]] = None,
    ):
        self.integration = integration
        self._bot_token = bot_token
        self._app_token = app_token
        self._api = api
        self._dispatch = dispatch
        self._connect = connect
        self._stopping = False
        self._ws: Any = None
        self._task: Optional[asyncio.Task] = None
        self._sleep = asyncio.sleep
        self.state = "connecting"
        self.connected_at: Optional[str] = None
        self.last_event_at: Optional[str] = None
        self.last_error: Optional[str] = None

    @property
    def integration_id(self) -> str:
        return self.integration["integration_id"]

    def start(self) -> None:
        self._task = asyncio.create_task(self.run(), name=f"slack-{self.integration_id}")

    def status(self) -> Dict[str, Any]:
        return {
            "state": self.state,
            "connected_at": self.connected_at,
            "last_event_at": self.last_event_at,
            "last_error": self.last_error,
        }

    async def stop(self) -> None:
        """Stop receiving. Turns already handed off are the manager's to drain."""
        self._stopping = True
        self.state = "stopped"
        ws = self._ws
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                pass
        if self._task is not None and not self._task.done():
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    async def run(self) -> None:
        backoff = 1.0
        while not self._stopping:
            reconnect_now = False
            try:
                self.state = "connecting"
                opened = await self._api.open_connection(self._app_token)
                url = str(opened.data.get("url") or "")
                if not url:
                    raise SlackApiError("apps.connections.open", "bad_response")
                reconnect_now = await self._session(url)
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except SlackApiError as exc:
                self.last_error = exc.error
                if exc.is_auth_error:
                    # Permanent until someone re-links the app.
                    self.state = "auth_error"
                    logger.warning(
                        "Slack integration %s: token refused (%s); not retrying",
                        self.integration_id, exc.error,
                    )
                    return
            except Exception as exc:  # noqa: BLE001 — isolated per connection
                self.last_error = type(exc).__name__
                logger.warning("Slack integration %s connection error: %s", self.integration_id, type(exc).__name__)
            finally:
                self._ws = None
            if self._stopping:
                break
            self.state = "reconnecting"
            if not reconnect_now:
                await self._sleep(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)
        self.state = "stopped"

    async def _session(self, url: str) -> bool:
        """One WebSocket's life. True when Slack asked us to reconnect."""
        connect = self._connect
        if connect is None:
            import websockets

            connect = websockets.connect
        async with connect(url, open_timeout=15, close_timeout=2, ping_interval=30) as ws:
            self._ws = ws
            async for raw in ws:
                try:
                    message = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                kind = message.get("type")
                if kind == "hello":
                    self.state = "connected"
                    self.connected_at = _now_iso()
                    self.last_error = None
                    continue
                if kind == "disconnect":
                    logger.info(
                        "Slack integration %s: disconnect (%s)", self.integration_id, message.get("reason")
                    )
                    return True
                envelope_id = message.get("envelope_id")
                if envelope_id:
                    # Ack first, always: Slack's deadline is seconds.
                    await ws.send(json.dumps({"envelope_id": envelope_id}))
                if kind == "events_api" and isinstance(message.get("payload"), dict):
                    self.last_event_at = _now_iso()
                    self._dispatch(self.integration, self._bot_token, message["payload"])
        return False


class ConnectionManager:
    def __init__(
        self,
        *,
        repository: SlackRepository,
        api: SlackApi,
        handler: Any,
        load_tokens: Callable[[str], Tuple[str, str]],
        run_broker: Any = None,
        connect: Optional[Callable[..., Any]] = None,
        max_concurrent: int = MAX_CONCURRENT_TURNS,
    ):
        self.repository = repository
        self.api = api
        self.handler = handler
        self._load_tokens = load_tokens
        self._run_broker = run_broker
        self._connect = connect
        self._slots = asyncio.Semaphore(max_concurrent)
        # harness_id -> connection
        self._connections: Dict[str, SocketModeConnection] = {}
        # harness_id -> status of an integration that could not be connected
        self._failures: Dict[str, Dict[str, Any]] = {}
        # handler tasks: task -> harness_id
        self._turns: Dict[asyncio.Task, str] = {}
        self._reconcile_task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()
        self._receiving = True

    # ── lifecycle ───────────────────────────────────────────────────────────

    async def start(self, reconcile_seconds: float = RECONCILE_SECONDS) -> None:
        try:
            await self.reconcile()
        except Exception:
            logger.warning("Slack integrations could not be loaded at startup; retrying", exc_info=True)
        self._reconcile_task = asyncio.create_task(self._reconcile_forever(reconcile_seconds))

    async def _reconcile_forever(self, seconds: float) -> None:
        while True:
            await asyncio.sleep(seconds)
            try:
                await self.reconcile()
            except Exception:
                logger.warning("Slack reconcile failed", exc_info=True)

    async def reconcile(self) -> None:
        """Make the running connections match the table."""
        rows = await asyncio.to_thread(self.repository.list_integrations)
        active = {r["harness_id"]: r for r in rows if r.get("status") == STATUS_ACTIVE}
        for harness_id in list(self._connections):
            row = active.get(harness_id)
            if row is None or row.get("integration_id") != self._connections[harness_id].integration_id:
                await self.remove(harness_id)
        for harness_id in list(self._failures):
            if harness_id not in active:
                self._failures.pop(harness_id, None)
        for harness_id, row in active.items():
            if harness_id not in self._connections:
                await self.add(row)

    async def add(self, integration: Dict[str, Any]) -> None:
        if not self._receiving:
            return
        harness_id = integration["harness_id"]
        async with self._lock:
            if harness_id in self._connections:
                return
            try:
                bot_token, app_token = await asyncio.to_thread(self._load_tokens, integration["secret_arn"])
            except Exception as exc:  # noqa: BLE001
                logger.warning("Slack integration %s: tokens unavailable (%s)", harness_id, type(exc).__name__)
                self._failures[harness_id] = {
                    "state": "secret_error", "connected_at": None, "last_event_at": None,
                    "last_error": type(exc).__name__,
                }
                return
            self._failures.pop(harness_id, None)
            connection = SocketModeConnection(
                integration, bot_token, app_token, self.api, self._dispatch, connect=self._connect
            )
            self._connections[harness_id] = connection
            connection.start()
            logger.info("Slack integration %s connecting", harness_id)

    async def remove(self, harness_id: str, drain_seconds: float = DRAIN_SECONDS) -> None:
        """Stop receiving for this Harness, then stop and drain its turns."""
        async with self._lock:
            connection = self._connections.pop(harness_id, None)
        self._failures.pop(harness_id, None)
        if connection is not None:
            await connection.stop()
        await self._drain([t for t, h in self._turns.items() if h == harness_id], harness_id, drain_seconds)

    def status(self, harness_id: str) -> Dict[str, Any]:
        connection = self._connections.get(harness_id)
        if connection is not None:
            return connection.status()
        return self._failures.get(harness_id) or {
            "state": "disconnected", "connected_at": None, "last_event_at": None, "last_error": None,
        }

    async def stop_receiving(self) -> None:
        """Shutdown, step one: no new events. In-flight turns keep going."""
        self._receiving = False
        if self._reconcile_task is not None:
            self._reconcile_task.cancel()
            await asyncio.gather(self._reconcile_task, return_exceptions=True)
        connections = list(self._connections.values())
        self._connections.clear()
        await asyncio.gather(*(c.stop() for c in connections), return_exceptions=True)

    async def drain(self, timeout: float = DRAIN_SECONDS) -> None:
        """Shutdown, last step: give handlers time to post and unlock, then cancel."""
        tasks = list(self._turns)
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=timeout)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        await self.api.aclose()

    # ── turns ───────────────────────────────────────────────────────────────

    def _dispatch(self, integration: Dict[str, Any], bot_token: str, payload: Dict[str, Any]) -> None:
        task = asyncio.create_task(self._turn(integration, bot_token, payload))
        self._turns[task] = integration["harness_id"]
        task.add_done_callback(lambda t: self._turns.pop(t, None))

    async def _turn(self, integration, bot_token, payload) -> None:
        async with self._slots:
            try:
                await self.handler.handle(integration, bot_token, payload)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Slack DM handling failed for %s", integration.get("harness_id"))

    async def _drain(self, tasks: List[asyncio.Task], harness_id: str, timeout: float) -> None:
        if not tasks:
            return
        # Stop the runs, so each handler sees its stream end, posts the
        # "interrupted" note and releases its lock.
        if self._run_broker is not None:
            for thread_id, owner in list(getattr(self.handler, "active_threads", {}).items()):
                if owner == harness_id:
                    self._run_broker.cancel(thread_id)
        _, pending = await asyncio.wait(tasks, timeout=timeout)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
