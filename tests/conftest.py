"""Shared fixtures for the aiodisklavier test suite.

Tests run against a real aiohttp server that imitates the piano, rather than a mocking
layer. That keeps the suite independent of aiohttp internals, and it means assertions about
the query string are checking what genuinely went over the wire -- which matters, because
the firmware's valueless flag arguments are easy to encode wrongly.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from aiodisklavier import Disklavier
from aiodisklavier.const import (
    PATH_API_BASE,
    PATH_CURRENT_INFO,
    PATH_STATIC_INFO,
)

STATIC_INFO_PAYLOAD = {
    "api_version": "1.0",
    "api_revision": "1",
    "disklavier_id": "DKV000000000000",
    "enspire_region": "World",
    "enspire_version": "5.24.00",
    "enspire_model": "PRO",
    "piano_type": "grand",
}

CURRENT_INFO_PAYLOAD = {
    "power_status": "on",
    "quiet_status": "acoustic",
    "playback_status": "pause",
    "playback_position": "516000",
    "volume_main": "100",
    "song_title": "Beethoven - Symphony No. 7, Movement 1.",
    "song_artist": "Ludwig van Beethoven",
    "song_folder": "Liszt's Beethoven's Symphony No. 7",
    "song_length": "851900",
}

MASTER_PAYLOAD = {
    "repeat": "off",
    "seq": {
        "status": "pause",
        "time": 516000,
        "tempo": 100,
        # The real device reports both, and restore depends on them.
        "song_pfix": "y",
        "song_id": "24",
    },
    "vol": {"main": 100, "headphone": 70},
    "piano": {
        "quiet": "acoustic",
        "met_status": "disable",
        "met_tempo": "120",
        "met_beat": "4/4",
        "key_motion": "on",
    },
    "sbc": {"headphone": "disconnected", "usb": "connected"},
}


def dumps(payload: object) -> str:
    """Serialise a payload the way the firmware would."""
    return json.dumps(payload)


@dataclass
class Request:
    """One request the fake piano received."""

    path: str
    query_string: str
    query: dict[str, str]

    @property
    def command(self) -> str | None:
        """The open API command, for requests to the versioned ``/api/1.0/`` path."""
        prefix = f"{PATH_API_BASE}/"
        if not self.path.startswith(prefix):
            return None
        return self.path[len(prefix) :]


@dataclass
class FakePiano:
    """A stand-in for the piano's HTTP API."""

    requests: list[Request] = field(default_factory=list)

    #: Body returned by the next open API command. Defaults to an empty command reply.
    command_body: str = ""
    #: HTTP status returned by the next open API command.
    command_status: int = 200
    #: Extra headers on open API command replies, e.g. a Location for a redirect.
    command_headers: dict[str, str] = field(default_factory=dict)
    #: Body returned by /api/current_info.
    current_body: str = field(default_factory=lambda: dumps(CURRENT_INFO_PAYLOAD))
    #: Raw bytes for /api/current_info, taking precedence over ``current_body``. Lets a
    #: test serve byte-exact failure shapes, like a read cut inside a multibyte character.
    current_raw: bytes | None = None
    #: Called after each current_info request, to let a test change what comes next.
    on_current_info: Callable[[], None] | None = None
    #: Seconds to stall every response, for exercising client-side timeouts.
    delay: float = 0.0

    async def _record(self, request: web.Request) -> None:
        self.requests.append(
            Request(
                path=request.path,
                query_string=request.query_string,
                query=dict(request.query),
            )
        )
        if self.delay:
            await asyncio.sleep(self.delay)

    @property
    def last(self) -> Request:
        """The most recent request."""
        return self.requests[-1]

    def build_app(self) -> web.Application:
        """Build the aiohttp application."""

        async def static_info(request: web.Request) -> web.Response:
            await self._record(request)
            return web.Response(text=dumps(STATIC_INFO_PAYLOAD))

        async def current_info(request: web.Request) -> web.Response:
            await self._record(request)
            body = self.current_body
            raw = self.current_raw
            if self.on_current_info is not None:
                self.on_current_info()
            if raw is not None:
                return web.Response(body=raw)
            return web.Response(text=body)

        async def master_json(request: web.Request) -> web.Response:
            await self._record(request)
            return web.Response(text=dumps(MASTER_PAYLOAD))

        async def command(request: web.Request) -> web.Response:
            await self._record(request)
            return web.Response(
                text=self.command_body,
                status=self.command_status,
                headers=self.command_headers,
            )

        async def ctrl(request: web.Request) -> web.Response:
            await self._record(request)
            return web.Response(text="")

        app = web.Application()
        # Order matters: the specific state paths must be registered before the catch-all
        # command route, which would otherwise swallow them.
        app.router.add_get(PATH_STATIC_INFO, static_info)
        app.router.add_get(PATH_CURRENT_INFO, current_info)
        app.router.add_get(f"{PATH_API_BASE}/{{command}}", command)
        app.router.add_get("/ctrl/master.json", master_json)
        app.router.add_get("/ctrl/setSeq.php", ctrl)
        app.router.add_get("/ctrl/setSong.php", ctrl)
        app.router.add_get("/ctrl/putNoteOn.php", ctrl)
        app.router.add_get("/ctrl/setRefreshDB.php", ctrl)
        return app


@pytest.fixture
async def fake_piano() -> FakePiano:
    """Provide the fake piano's state and request log."""
    return FakePiano()


@pytest.fixture
async def server(fake_piano: FakePiano) -> AsyncIterator[TestServer]:
    """Run the fake piano."""
    test_server = TestServer(fake_piano.build_app())
    await test_server.start_server()
    try:
        yield test_server
    finally:
        await test_server.close()


@pytest.fixture
async def session() -> AsyncIterator[aiohttp.ClientSession]:
    """Provide an aiohttp session."""
    async with aiohttp.ClientSession() as client_session:
        yield client_session


@pytest.fixture
async def piano(server: TestServer, session: aiohttp.ClientSession) -> Disklavier:
    """Provide a client pointed at the fake piano."""
    return Disklavier(server.host, session, port=server.port)


@pytest.fixture
async def offline_piano(session: aiohttp.ClientSession) -> Disklavier:
    """Provide a client pointed at a port with nothing listening."""
    # Port 1 is reserved and never has a listener, so connections fail immediately.
    return Disklavier("127.0.0.1", session, port=1, timeout=2)
