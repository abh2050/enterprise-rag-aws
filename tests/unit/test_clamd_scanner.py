"""ClamdScanner protocol tests against an in-process fake clamd (INSTREAM framing + reply parsing)."""

from __future__ import annotations

import asyncio

from erp_connectors.scanning import EICAR, ClamdScanner


async def fake_clamd(reply_for: dict[str, bytes]) -> tuple[asyncio.AbstractServer, int, list[bytes]]:
    received: list[bytes] = []

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        assert await reader.readexactly(10) == b"zINSTREAM\0"
        body = b""
        while True:
            size = int.from_bytes(await reader.readexactly(4), "big")
            if size == 0:
                break
            body += await reader.readexactly(size)
        received.append(body)
        key = "eicar" if EICAR in body else "big" if len(body) > 200_000 else "clean"
        writer.write(reply_for[key])
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    return server, port, received


async def test_clean_infected_and_error_replies() -> None:
    server, port, received = await fake_clamd(
        {
            "clean": b"stream: OK\0",
            "eicar": b"stream: Eicar-Signature FOUND\0",
            "big": b"INSTREAM size limit exceeded. ERROR\0",
        }
    )
    async with server:
        scanner = ClamdScanner("127.0.0.1", port)
        clean = await scanner.scan(b"hello world" * 10_000, artifact_key="k")  # spans several 64 KiB chunks
        infected = await scanner.scan(b"prefix " + EICAR, artifact_key="k")
        too_big = await scanner.scan(b"x" * 300_000, artifact_key="k")
    assert clean.clean and received[0] == b"hello world" * 10_000
    assert infected.status == "infected" and infected.detail == "Eicar-Signature"
    assert too_big.status == "error" and not too_big.clean  # never treated as clean


async def test_unreachable_daemon_is_not_clean() -> None:
    result = await ClamdScanner("127.0.0.1", 1, timeout_s=2).scan(b"data", artifact_key="k")
    assert result.status == "error" and not result.clean
