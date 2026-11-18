"""Диагностика внутреннего вызова delivery -> auth (запускается внутри контейнера delivery).

    docker compose exec -T delivery python - < runner/diag_auth.py

Сравнивает время 300 последовательных вызовов /internal/verify через httpx (как в сервисе)
и через «сырой» сокет с TCP_NODELAY и без него. Нужен для поиска задержек сетевого стека.
"""
import asyncio
import json
import socket
import statistics
import time
import urllib.request

import httpx

BASE = "http://auth:8000"
N = 300


def token() -> str:
    req = urllib.request.Request(BASE + "/api/v1/sessions", data=b"{}", method="POST",
                                 headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req).read())["token"]


def stats(name: str, xs: list[float]) -> None:
    xs = sorted(x * 1000 for x in xs)
    print(f"{name:34s} p50={statistics.median(xs):6.2f} ms  p90={xs[int(0.9 * len(xs))]:6.2f}  "
          f"p99={xs[int(0.99 * len(xs))]:6.2f}  max={xs[-1]:6.2f}")


async def via_httpx(tok: str) -> list[float]:
    out = []
    async with httpx.AsyncClient(base_url=BASE) as c:
        for _ in range(N):
            t = time.perf_counter()
            r = await c.get("/internal/verify", params={"token": tok})
            out.append(time.perf_counter() - t)
            assert r.status_code == 204
    return out


def via_socket(tok: str, nodelay: bool) -> list[float]:
    s = socket.create_connection(("auth", 8000))
    s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1 if nodelay else 0)
    req = f"GET /internal/verify?token={tok} HTTP/1.1\r\nHost: auth\r\n\r\n".encode()
    out = []
    for _ in range(N):
        t = time.perf_counter()
        s.sendall(req)
        buf = b""
        while b"\r\n\r\n" not in buf:
            buf += s.recv(4096)
        out.append(time.perf_counter() - t)
    s.close()
    return out


if __name__ == "__main__":
    tok = token()
    stats("httpx AsyncClient (как в сервисе)", asyncio.run(via_httpx(tok)))
    stats("сокет, TCP_NODELAY=1", via_socket(tok, True))
    stats("сокет, TCP_NODELAY=0", via_socket(tok, False))
