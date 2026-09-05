# fast-balancer

A tiny **load balancer / reverse proxy** built with FastAPI.

It sits in front of your backend servers and sends each incoming request to one of them,
rotating between them so no single server gets overloaded. If a server crashes or goes
offline, it is skipped automatically, and when it comes back it is used again.

## What the code does

- **Balances traffic** — Incoming requests are forwarded to the backends one at a time
  (round-robin style: backend 1, then backend 2, then back to backend 1…).
- **Checks backend health** — A background task pings every backend on a schedule and keeps
  a list of the ones that are alive.
  - A backend is considered **down** only if the connection fails (no response at all).
  - Even an error response like 404 or 500 counts as healthy — it means the server is reachable.
  - Backends that recover are automatically added back.
- **Forwards requests** — The proxy accepts any request (any path, any method), cleans up
  proxy-only headers, sends it to the chosen backend, and streams the reply straight back.
- **Handles failure gracefully** — Returns `503` if every backend is down, or `502` if the
  chosen backend dies mid-request.

The backends it balances between are listed at the top of [main.py](main.py):

```python
_KNOWN_BACKENDS = [
    "http://localhost:8001",
    "http://localhost:8002"
]
```

You can change these to point at your own servers.

## Requirements

- Python 3.14+
- [uv](https://docs.astral.sh/uv/) (or use `pip` instead)

## How to run

### 1. Install the dependencies

```bash
uv sync
```

### 2. Start it

```bash
uv run uvicorn main:app --port 8000 --reload
```

The balancer now listens on `http://localhost:8000`.

### 3. Have something running on the backend ports

The balancer forwards to whatever is on `localhost:8001` and `localhost:8002`.
For a quick test you can start two tiny backends, for example:

```bash
# Terminal A — a backend on :8001
python -m http.server 8001
```

```bash
# Terminal B — a backend on :8002
python -m http.server 8002
```

### 4. Test it

Point the browser (or `curl`) at the balancer and watch requests alternate between backends:

```bash
curl http://localhost:8000/anything
curl http://localhost:8000/anything
```

If you open two different backends that return different content, you'll see the responses
swap on each request. Try stopping one backend — it stops receiving traffic, and when you
restart it, it comes back.

## Configuration (optional)

The health check is tunable with environment variables:

| Variable           | Default | What it does                                    |
| ------------------ | ------- | ----------------------------------------------- |
| `HEALTH_INTERVAL`  | `5.0`   | Seconds between health-check passes             |
| `HEALTH_TIMEOUT`   | `3.0`   | Seconds to wait per health-check request        |

```bash
HEALTH_INTERVAL=10 uv run uvicorn main:app --port 8000
```

## FAQ

### Why use `@app.api_route(...)` instead of `@app.get("/")` or `@app.post("/")`?

Because this app is a **proxy** — it has to accept any request and forward it, no matter the
path or the HTTP method.

- `@app.get("/")` only matches a `GET` on the root path.
- `@app.post("/")` only matches a `POST` on the root path.

Those would reject everything else. So instead the code registers one route that catches
everything:

```python
@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"])
```

Two parts matter:

- `"/{path:path}"` — the `:path` converter means "capture the rest of the URL", so a request
  to `/anything/at/all` is matched and the rest of the path is passed along to the backend.
- `methods=[...]` — lists every HTTP method the balancer will accept.

The handler never cares what the request is; it reads it and forwards it to a backend
(see [main.py](main.py)). That is exactly the job `api_route` was made for.

### Why use `StreamingResponse` instead of a plain `Response`?

Because the balancer is passing through data it didn't create — it's a middleman between the
client and a backend. A plain `Response` would force it to download the **whole** backend reply
into memory first, and only then send it to the client. For a large file or slow response that
means high memory usage and a long wait before the client sees anything.

`StreamingResponse` solves that by sending each chunk of data to the client **as soon as it
arrives** from the backend:

```python
async def stream():
    async for chunk in response.aiter_bytes():
        yield chunk          # hand each chunk to the client right away

return StreamingResponse(stream(), status_code=..., headers=...)
```

So instead of "download everything, then send everything", it becomes a live pipe:
backend → balancer → client, a little at a time.

That matters for two reasons:

- **Memory** — only one chunk is held at a time, no matter how big the response is.
- **Speed (latency)** — the client gets the first bytes sooner, which is essential for big
  downloads, videos, or responses that stream over time (like server-sent events).

The proxy also sends the request to the backend with `stream=True` (see [main.py](main.py)),
so neither side buffers the whole body — the connection stays a continuous stream end to end.

## File layout

```
main.py              ← the whole balancer lives here
pyproject.toml       ← project setup and dependencies
src/fast_balancer/   ← packaging entry point (not used by the main app)
```
