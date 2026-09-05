import asyncio
import contextlib
import os
import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import StreamingResponse

# Ordered list of every configured backend. This is what the health checker
# pings; recovered nodes are re-inserted at their original position.
_KNOWN_BACKENDS = [
    "http://localhost:8001",
    "http://localhost:8002"
]

# Live rotation: the ordered subset of _KNOWN_BACKENDS that passed their last
# health check. Mutated in place by the background task.
BACKENDS = list(_KNOWN_BACKENDS)

_current_backend = 0
_backends_lock = asyncio.Lock()

HEALTH_INTERVAL = float(os.getenv("HEALTH_INTERVAL", "5.0"))  # seconds between passes
HEALTH_TIMEOUT = float(os.getenv("HEALTH_TIMEOUT", "3.0"))    # per-request health ping budget
HEALTH_PATH = "/api/v2/health-check"


async def _is_healthy(client: httpx.AsyncClient, backend: str) -> bool:
    """A backend is healthy unless the connection itself fails.

    Any HTTP response (even 404/500) proves the server is reachable.
    """
    try:
        await client.get(f"{backend.rstrip('/')}{HEALTH_PATH}")
        return True
    except httpx.RequestError:
        return False


async def _health_check_pass(client: httpx.AsyncClient) -> None:
    """Ping every known backend once and refresh BACKENDS to the healthy subset."""
    async with _backends_lock:
        current = set(BACKENDS)
    healthy = set()
    for backend in _KNOWN_BACKENDS:
        if await _is_healthy(client, backend):
            healthy.add(backend)
        elif backend in current:
            print(f"[health] backend down: {backend}")

    async with _backends_lock:
        for backend in _KNOWN_BACKENDS:
            if backend in healthy and backend not in current:
                print(f"[health] backend up: {backend}")
        BACKENDS[:] = [b for b in _KNOWN_BACKENDS if b in healthy]


async def health_check_loop() -> None:
    """Background task: run a check pass immediately, then on HEALTH_INTERVAL."""
    async with httpx.AsyncClient(timeout=HEALTH_TIMEOUT) as client:
        while True:
            try:
                await _health_check_pass(client)
            except asyncio.CancelledError:
                raise
            await asyncio.sleep(HEALTH_INTERVAL)


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(health_check_loop())
    yield
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


app = FastAPI(lifespan=lifespan)


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"])
async def proxy(request: Request, path: str):
    global _current_backend
    async with _backends_lock:
        if not BACKENDS:
            return Response(content="No healthy backends available", status_code=503)
        backend = BACKENDS[_current_backend % len(BACKENDS)]
        _current_backend = (_current_backend + 1) % len(BACKENDS)

    url = f"{backend}/{path}"
    client = httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0))

    body = await request.body()
    HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate",
    "proxy-authorization", "te", "trailer", "transfer-encoding", "upgrade"
    }
    headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP_BY_HOP}
    headers.pop("host", None)

    try:
        req = client.build_request(
            request.method,
            url,
            headers=headers,
            content=body,
            params=request.query_params
        )
        response = await client.send(req, stream=True)
    except httpx.RequestError:
        await client.aclose()
        return Response(content="Bad Gateway: Backend unavailable", status_code=502)

    async def stream():
        try:
            async for chunk in response.aiter_bytes():
                yield chunk
        finally:
            await client.aclose()

    return StreamingResponse(
        stream(),
        status_code=response.status_code,
        headers=dict(response.headers),
    )
