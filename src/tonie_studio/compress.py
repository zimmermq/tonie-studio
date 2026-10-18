"""Compressing the pages: brotli where the browser takes it, gzip otherwise.

The pages are mostly the same markup row after row - the print page of every
tonie is about a megabyte, the tonie list half of that - which both shrink to
a tenth or less. Pictures, PDFs and zips are compressed already and pass as
they are, and so does anything streamed: a page is held whole while it is
compressed, a download of hundreds of megabytes must not be.
"""

from __future__ import annotations

import gzip
from typing import List, Optional

import brotlicffi

# What is worth compressing, by the start of its media type.
COMPRESSIBLE = ("text/", "application/json", "application/javascript", "image/svg+xml")

# Below this many bytes the headers and the work outweigh what is saved.
SMALLEST = 1024

# Brotli 5 compresses pages about as well as 11 at a fraction of the time;
# pages are made per request, not once ahead.
BROTLI_QUALITY = 5
GZIP_LEVEL = 6


def encode(body: bytes, coding: str) -> bytes:
    """``body`` in ``coding``, "br" or "gzip"."""
    if coding == "br":
        return brotlicffi.compress(body, quality=BROTLI_QUALITY)
    return gzip.compress(body, compresslevel=GZIP_LEVEL)


def chosen_coding(accept_encoding: str) -> Optional[str]:
    """The coding to answer in: br before gzip, as far as the request accepts either."""
    offered = {}
    for part in accept_encoding.split(","):
        name, _semicolon, params = part.strip().partition(";")
        weight = 1.0
        for param in params.split(";"):
            key, _equals, value = param.strip().partition("=")
            if key == "q":
                try:
                    weight = float(value)
                except ValueError:
                    weight = 0.0
        offered[name.strip().lower()] = weight
    for coding in ("br", "gzip"):
        if offered.get(coding, offered.get("*", 0.0)) > 0:
            return coding
    return None


class CompressMiddleware:
    """An ASGI middleware compressing whole, compressible answers."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        coding = chosen_coding(headers.get(b"accept-encoding", b"").decode("latin-1"))
        if coding is None:
            await self.app(scope, receive, send)
            return

        start: Optional[dict] = None
        chunks: List[bytes] = []
        passing = False

        done = False

        async def wrapped(message):
            nonlocal start, passing, done
            if message["type"] == "http.response.start":
                start = message
                passing = not _compressible(message)
                if passing:
                    await send(message)
                return
            if passing or start is None or done or message["type"] != "http.response.body":
                await send(message)
                return
            chunks.append(message.get("body", b""))
            if message.get("more_body"):
                # Streamed: hand on what was held and pass the rest as it comes.
                passing = True
                await send(start)
                await send({"type": "http.response.body", "body": b"".join(chunks),
                            "more_body": True})
                return
            done = True
            await _send_whole(send, start, b"".join(chunks), coding)

        await self.app(scope, receive, wrapped)


def _compressible(start: dict) -> bool:
    headers = {key.lower(): value for key, value in start.get("headers", [])}
    if start.get("status", 200) in (204, 304) or b"content-encoding" in headers:
        return False
    media = headers.get(b"content-type", b"").decode("latin-1").lower()
    return media.startswith(COMPRESSIBLE)


async def _send_whole(send, start: dict, body: bytes, coding: str) -> None:
    headers = [(key, value) for key, value in start.get("headers", [])
               if key.lower() not in (b"content-length", b"vary")]
    vary = [value for key, value in start.get("headers", []) if key.lower() == b"vary"]
    headers.append((b"vary", b", ".join(vary + [b"Accept-Encoding"])))
    if len(body) >= SMALLEST:
        body = encode(body, coding)
        headers.append((b"content-encoding", coding.encode()))
    headers.append((b"content-length", str(len(body)).encode()))
    await send({**start, "headers": headers})
    await send({"type": "http.response.body", "body": body})
