import base64
import threading
import time
import uuid
from collections import defaultdict, deque
from typing import Optional

from fastapi import FastAPI, Header, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

TOTAL_ORDERS = 46          # T
RATE_LIMIT = 16            # R requests ...
WINDOW_SECONDS = 10        # ... per 10 seconds

app = FastAPI(title="Orders API")

# ---------------- Rate limiting (per X-Client-Id, sliding window) ----------------
_hits = defaultdict(deque)
_hits_lock = threading.Lock()


@app.middleware("http")
async def rate_limit(request: Request, call_next):
    client_id = request.headers.get("x-client-id")
    if client_id and request.method != "OPTIONS":
        now = time.monotonic()
        with _hits_lock:
            q = _hits[client_id]
            while q and now - q[0] >= WINDOW_SECONDS:
                q.popleft()
            if len(q) >= RATE_LIMIT:
                retry_after = max(1, int(WINDOW_SECONDS - (now - q[0])) + 1)
                return JSONResponse(
                    status_code=429,
                    content={"detail": "Too Many Requests"},
                    headers={"Retry-After": str(retry_after)},
                )
            q.append(now)
    return await call_next(request)


# CORS added last => outermost, so 429 responses also carry CORS headers.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Retry-After"],
)

# ---------------- Idempotent order creation ----------------
_orders_by_key = {}
_created_orders = {}
_orders_lock = threading.Lock()


@app.post("/orders", status_code=201)
async def create_order(request: Request, idempotency_key: Optional[str] = Header(None)):
    try:
        body = await request.json()
        if not isinstance(body, dict):
            body = {}
    except Exception:
        body = {}

    with _orders_lock:
        if idempotency_key and idempotency_key in _orders_by_key:
            return JSONResponse(status_code=201, content=_orders_by_key[idempotency_key])
        order = {
            "id": f"ord_{uuid.uuid4().hex[:12]}",
            "status": "created",
            "created_at": int(time.time()),
            **{k: v for k, v in body.items() if k not in ("id", "status", "created_at")},
        }
        _created_orders[order["id"]] = order
        if idempotency_key:
            _orders_by_key[idempotency_key] = order
        return JSONResponse(status_code=201, content=order)


# ---------------- Cursor pagination over fixed catalog 1..T ----------------
def _encode(offset: int) -> str:
    return base64.urlsafe_b64encode(f"o:{offset}".encode()).decode().rstrip("=")


def _decode(cursor: str) -> int:
    pad = "=" * (-len(cursor) % 4)
    raw = base64.urlsafe_b64decode(cursor + pad).decode()
    if not raw.startswith("o:"):
        raise ValueError
    off = int(raw[2:])
    if off < 0:
        raise ValueError
    return off


@app.get("/orders")
def list_orders(limit: int = Query(10, ge=1, le=1000), cursor: Optional[str] = None):
    offset = 0
    if cursor:
        try:
            offset = _decode(cursor)
        except Exception:
            return JSONResponse(status_code=400, content={"detail": "Invalid cursor"})
    start = offset + 1                      # IDs are 1..T
    end = min(start + limit - 1, TOTAL_ORDERS)
    items = [{"id": i, "status": "created", "item": f"item-{i}"} for i in range(start, end + 1)]
    next_cursor = _encode(end) if end < TOTAL_ORDERS else None
    return {"items": items, "next_cursor": next_cursor}


@app.get("/")
def health():
    return {"status": "ok"}
