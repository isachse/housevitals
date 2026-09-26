"""stdio -> Streamable HTTP bridge to the running housevitals service.

MCP clients that only start local (stdio) servers, such as Claude Desktop, run
`housevitals-mcp` with HOUSEVITALS_URL (or --url). Every JSON-RPC message from stdin
is forwarded unchanged to the service's /mcp endpoint and every answer written to
stdout, so the client shares the service's cache, poller, history and charts and
never opens Modbus connections of its own.

- The service restarted (session unknown, HTTP 404): the bridge initializes a new
  session with the client's original initialize request and retries the call.
- The service is unreachable: requests get a JSON-RPC error saying so at once.
- Server-initiated messages (GET stream) are not bridged; the service sends none.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

_LOGGER = logging.getLogger(__name__)

PROTOCOL_HEADER = "mcp-protocol-version"
SESSION_HEADER = "mcp-session-id"
TIMEOUT = httpx.Timeout(60.0, connect=3.0)  # charts may take a few seconds to render
UNREACHABLE = -32000


def health_url(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, "/healthz", "", ""))


def service_reachable(url: str, timeout: float = 2.0) -> bool:
    try:
        return httpx.get(health_url(url), timeout=timeout).status_code == 200
    except httpx.HTTPError:
        return False


class Bridge:
    """Forwards JSON-RPC messages to the service and returns its answers."""

    def __init__(self, url: str, transport: httpx.AsyncBaseTransport | None = None):
        self.url = url
        self._http = httpx.AsyncClient(timeout=TIMEOUT, transport=transport)
        self.session_id: str | None = None
        self.protocol_version: str | None = None
        self._initialize: dict | None = None  # the client's initialize request, for re-init
        self._reinit = asyncio.Lock()

    async def close(self) -> None:
        if self.session_id:
            try:
                await self._http.delete(self.url, headers=self._headers())
            except httpx.HTTPError:
                pass
        await self._http.aclose()

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json, text/event-stream",
                   "Content-Type": "application/json"}
        if self.session_id:
            headers[SESSION_HEADER] = self.session_id
        if self.protocol_version:
            headers[PROTOCOL_HEADER] = self.protocol_version
        return headers

    async def _post(self, message: dict) -> tuple[int, list[dict], str | None]:
        """POST one message; returns (status, answers, session id from the response)."""
        async with self._http.stream("POST", self.url, json=message, headers=self._headers()) as resp:
            session = resp.headers.get(SESSION_HEADER)
            if resp.status_code == 202:
                return 202, [], session
            ctype = resp.headers.get("content-type", "")
            if ctype.startswith("text/event-stream"):
                answers = []
                data: list[str] = []
                async for line in resp.aiter_lines():
                    if line.startswith("data:"):
                        data.append(line[5:].lstrip())
                    elif not line and data:
                        answers.append(json.loads("\n".join(data)))
                        data = []
                if data:
                    answers.append(json.loads("\n".join(data)))
                return resp.status_code, answers, session
            body = await resp.aread()
            answers = [json.loads(body)] if body.strip() else []
            return resp.status_code, answers, session

    async def _start_session(self, initialize: dict) -> list[dict]:
        self.session_id = None
        status, answers, session = await self._post(initialize)
        if session:
            self.session_id = session
        for answer in answers:
            version = answer.get("result", {}).get("protocolVersion")
            if version:
                self.protocol_version = version
        return answers

    async def _reinitialize(self, stale_session: str | None) -> None:
        async with self._reinit:
            if self.session_id != stale_session:  # another request already did it
                return
            _LOGGER.warning("housevitals session expired (service restarted?); starting a new one")
            await self._start_session({**self._initialize, "id": "housevitals-proxy-reinit"})
            await self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})

    async def handle(self, message: dict) -> list[dict]:
        """Forward one client message; returns the messages to write back."""
        request_id = message.get("id")
        is_request = "method" in message and request_id is not None
        try:
            if message.get("method") == "initialize":
                self._initialize = message
                return await self._start_session(message)
            for attempt in (1, 2):
                session = self.session_id
                status, answers, _ = await self._post(message)
                if status == 404 and session and self._initialize and attempt == 1:
                    await self._reinitialize(session)
                    continue
                return answers
            return answers
        except (httpx.HTTPError, OSError) as err:
            _LOGGER.warning("housevitals service not reachable at %s: %s", self.url, err)
            if not is_request:
                return []
            return [{"jsonrpc": "2.0", "id": request_id, "error": {
                "code": UNREACHABLE,
                "message": (f"The housevitals service at {self.url} is not reachable "
                            f"({type(err).__name__}). It runs as launchd agent "
                            "local.housevitals; try again in a moment."),
            }}]


async def run(url: str) -> None:
    """Bridge stdin/stdout (newline-delimited JSON-RPC) to the service."""
    bridge = Bridge(url)
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
    out_lock = asyncio.Lock()
    pending: set[asyncio.Task] = set()

    async def write(messages: list[dict]) -> None:
        async with out_lock:
            for m in messages:
                sys.stdout.write(json.dumps(m, ensure_ascii=False) + "\n")
            sys.stdout.flush()

    async def forward(message: dict) -> None:
        await write(await bridge.handle(message))

    try:
        while line := await reader.readline():
            if not line.strip():
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                _LOGGER.warning("ignoring invalid JSON from the client")
                continue
            if message.get("method") == "initialize":
                await forward(message)  # everything else needs the session
                continue
            task = asyncio.create_task(forward(message))  # tool calls run concurrently
            pending.add(task)
            task.add_done_callback(pending.discard)
    finally:
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        await bridge.close()
