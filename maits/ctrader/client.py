"""asyncio wrapper around the Twisted-based cTrader Open API client.

Responsibilities: connect, application auth, account auth, re-auth after reconnects, keeping the
OAuth token fresh, request/response matching (awaitable), and fan-out of server events
(execution events, spot prices, ...) to listeners.
"""
import asyncio
import logging
import time
import uuid
from collections.abc import Callable

from ctrader_open_api import Client, EndPoints, Protobuf, TcpProtocol
from ctrader_open_api.messages import OpenApiMessages_pb2 as oa
from ctrader_open_api.messages.OpenApiCommonMessages_pb2 import ProtoErrorRes, ProtoHeartbeatEvent
from twisted.internet import defer

from maits.config import Settings
from maits.ctrader.auth import TokenStore, refresh_tokens

log = logging.getLogger(__name__)

HEARTBEAT = ProtoHeartbeatEvent().payloadType
ERROR_TYPES = (oa.ProtoOAErrorRes, ProtoErrorRes, oa.ProtoOAOrderErrorEvent)
REFRESH_WHEN_LEFT = 3 * 86400  # refresh the access token when < 3 days are left


class CTraderError(Exception):
    def __init__(self, code: str, description: str = ""):
        super().__init__(f"{code}: {description}" if description else code)
        self.code = code
        self.description = description


class _SdkClient(Client):
    """The stock client queues every message and flushes the queue once a second, which adds up
    to a second of latency. `instant=True` writes to the socket immediately (used for orders)."""

    def send(self, message, clientMsgId=None, responseTimeoutInSeconds=5, instant=False, **params):
        if not instant:
            return super().send(message, clientMsgId, responseTimeoutInSeconds, **params)
        deferred = defer.Deferred(self._cancelMessageDiferred)
        self._responseDeferreds[clientMsgId] = deferred
        deferred.addErrback(lambda failure: self._onResponseFailure(failure, clientMsgId))
        deferred.addTimeout(responseTimeoutInSeconds, self._runningReactor)
        self.whenConnected(failAfterFailures=1).addCallbacks(
            lambda protocol: protocol.send(message, instant=True, clientMsgId=clientMsgId), deferred.errback
        )
        return deferred


class CTraderClient:
    def __init__(self, settings: Settings, store: TokenStore):
        self._settings = settings
        self._store = store
        host = EndPoints.PROTOBUF_LIVE_HOST if settings.is_live else EndPoints.PROTOBUF_DEMO_HOST
        self._sdk = _SdkClient(host, EndPoints.PROTOBUF_PORT, TcpProtocol, numberOfMessagesToSendPerSecond=20)
        self._sdk.setConnectedCallback(self._on_connected)
        self._sdk.setDisconnectedCallback(self._on_disconnected)
        self._sdk.setMessageReceivedCallback(self._on_message)
        self._account_id: int | None = None
        self._app_ready = asyncio.Event()  # connected + application authenticated
        self._ready = asyncio.Event()  # ... and account authenticated
        self._first_auth: asyncio.Future | None = None
        self._listeners: list[Callable] = []
        self._tasks: set[asyncio.Task] = set()
        self._token_lock = asyncio.Lock()

    # ---- lifecycle -------------------------------------------------------------------------

    async def start(self, account_id: int | None = None) -> None:
        """Connect and authenticate the application (and the account, if given)."""
        self._account_id = account_id
        self._first_auth = asyncio.get_running_loop().create_future()
        self._sdk.startService()
        try:
            await asyncio.wait_for(asyncio.shield(self._first_auth), 30)
        except asyncio.TimeoutError:
            raise CTraderError("TIMEOUT", "could not connect and authenticate within 30s") from None
        self._spawn(self._token_refresh_loop())

    @property
    def is_ready(self) -> bool:
        return self._ready.is_set()

    async def authorize_account(self, account_id: int) -> None:
        self._account_id = account_id
        await self._authenticate(app=False, propagate=True)

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        self._sdk.stopService()

    def _spawn(self, coro) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # ---- authentication --------------------------------------------------------------------

    async def access_token(self) -> str:
        """Current access token, refreshed (and saved) first if it is about to expire."""
        async with self._token_lock:
            tokens = self._store.load()
            if tokens is None:
                raise CTraderError("NO_TOKENS", "no OAuth tokens found - run `maits auth` first")
            if tokens.seconds_left < REFRESH_WHEN_LEFT:
                log.info("refreshing access token (%.1f days left)", tokens.seconds_left / 86400)
                tokens = await refresh_tokens(self._settings, tokens)
                self._store.save(tokens)
            return tokens.access_token

    async def _token_refresh_loop(self) -> None:
        while True:
            await asyncio.sleep(3600)
            try:
                await self.access_token()
            except Exception:
                log.exception("token refresh failed")

    async def _authenticate(self, app: bool = True, propagate: bool = False) -> None:
        """Authenticate the application (`app`) and then the account, if one is chosen.

        A failure goes to whoever is waiting: `start()` on the first attempt, the caller when
        `propagate` is set; otherwise (reconnects, server-initiated re-auth) it is only logged.
        """
        first = self._first_auth
        try:
            self._ready.clear()
            if app:
                self._app_ready.clear()
                await self._send(
                    oa.ProtoOAApplicationAuthReq(
                        clientId=self._settings.client_id, clientSecret=self._settings.client_secret
                    )
                )
                self._app_ready.set()
            if self._account_id is not None:
                await self._send(
                    oa.ProtoOAAccountAuthReq(
                        ctidTraderAccountId=self._account_id, accessToken=await self.access_token()
                    )
                )
                self._ready.set()
                log.info("account %s authenticated", self._account_id)
            if first and not first.done():
                first.set_result(None)
        except Exception as exc:
            if first and not first.done():
                first.set_exception(exc)
            elif propagate:
                raise
            else:
                log.error("re-authentication failed: %s", exc)

    # ---- Twisted callbacks (they run on the asyncio loop) ------------------------------------

    def _on_connected(self, _client) -> None:
        log.info("connected to cTrader")
        self._spawn(self._authenticate())

    def _on_disconnected(self, _client, reason) -> None:
        log.warning("disconnected from cTrader: %s (the SDK will reconnect)", reason.getErrorMessage())
        self._app_ready.clear()
        self._ready.clear()

    def _on_message(self, _client, raw) -> None:
        if raw.payloadType == HEARTBEAT:
            return
        try:
            payload = Protobuf.extract(raw)
        except Exception:
            log.exception("could not decode message type %s", raw.payloadType)
            return
        if isinstance(payload, oa.ProtoOAAccountDisconnectEvent):
            log.warning("server ended the account session - re-authenticating")
            self._spawn(self._authenticate(app=False))
        elif isinstance(payload, (oa.ProtoOAAccountsTokenInvalidatedEvent, oa.ProtoOAClientDisconnectEvent)):
            log.warning("server event: %s", payload)
        for listener in list(self._listeners):
            try:
                listener(payload)
            except Exception:
                log.exception("listener failed")

    # ---- requests & events -----------------------------------------------------------------

    def subscribe(self, callback: Callable) -> None:
        """callback(payload) is called for every message from the server (responses included)."""
        self._listeners.append(callback)

    def unsubscribe(self, callback: Callable) -> None:
        if callback in self._listeners:
            self._listeners.remove(callback)

    async def request(self, message, *, instant: bool = False, timeout: float = 10):
        """Send a request and return the decoded response. Raises CTraderError on error responses.

        Waits until the connection is authenticated (requests carrying an account id also wait for
        account auth), so callers don't need to care about reconnects.
        """
        needs_account = "ctidTraderAccountId" in message.DESCRIPTOR.fields_by_name
        gate = self._ready if needs_account else self._app_ready
        try:
            await asyncio.wait_for(gate.wait(), 15)
        except asyncio.TimeoutError:
            raise CTraderError("NOT_CONNECTED", "not connected/authenticated to cTrader") from None
        return await self._send(message, instant=instant, timeout=timeout)

    async def _send(self, message, *, instant: bool = False, timeout: float = 10):
        deferred = self._sdk.send(
            message, clientMsgId=uuid.uuid4().hex, responseTimeoutInSeconds=timeout, instant=instant
        )
        try:
            raw = await deferred.asFuture(asyncio.get_running_loop())
        except defer.TimeoutError:
            raise CTraderError("TIMEOUT", f"no response to {type(message).__name__} within {timeout}s") from None
        payload = Protobuf.extract(raw)
        if isinstance(payload, ERROR_TYPES):
            raise CTraderError(payload.errorCode, payload.description)
        return payload
