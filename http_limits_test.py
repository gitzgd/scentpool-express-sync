"""Synthetic real-socket regressions; never contacts a provider or production."""

from __future__ import annotations

import contextlib
import json
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, HTTPServer

from http_limits import HttpRequestError, RequestReadLimitsMixin, reject_overloaded_request


class Handler(RequestReadLimitsMixin, BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    header_read_timeout = 0.3
    body_read_timeout = 0.3
    upload_read_timeout = 0.8
    response_write_timeout = 2.0
    max_header_bytes = 1024
    max_normal_body_bytes = 64
    max_upload_body_bytes = 256

    def log_message(self, *_args: object) -> None:
        pass

    def send_result(self, status: int, payload: dict) -> None:
        raw = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(raw)
        self.close_connection = True

    def do_GET(self) -> None:
        if self.path == "/slow-response":
            time.sleep(0.5)  # Longer than either receive budget, intentionally.
        self.send_result(200, {"ok": True, "socket_timeout": self.connection.gettimeout()})

    def do_POST(self) -> None:
        try:
            body = self.read_request_body(256)
            if self.path == "/slow-response":
                time.sleep(0.5)
            self.send_result(200, {"received": len(body), "socket_timeout": self.connection.gettimeout()})
        except HttpRequestError as exc:
            self.send_result(exc.status, {"error": exc.message})


class Pool(HTTPServer):
    def __init__(self) -> None:
        self.slots = threading.BoundedSemaphore(2)
        self.executor = ThreadPoolExecutor(max_workers=2)
        super().__init__(("127.0.0.1", 0), Handler)

    def process_request(self, request: socket.socket, address: tuple) -> None:
        if not self.slots.acquire(blocking=False):
            reject_overloaded_request(request)
            return
        try:
            self.executor.submit(self.run_request, request, address)
        except Exception:
            self.slots.release()
            self.shutdown_request(request)
            raise

    def run_request(self, request: socket.socket, address: tuple) -> None:
        try:
            self.finish_request(request, address)
        finally:
            self.shutdown_request(request)
            self.slots.release()

    def server_close(self) -> None:
        super().server_close()
        self.executor.shutdown(wait=True)


def receive(sock: socket.socket) -> bytes:
    chunks = []
    while True:
        try:
            part = sock.recv(8192)
        except ConnectionResetError:
            break
        if not part:
            break
        chunks.append(part)
    return b"".join(chunks)


def status(response: bytes) -> int:
    assert b"PRIVATE" not in response, response
    return int(response.split(b" ", 2)[1])


def connect(server: Pool) -> socket.socket:
    return socket.create_connection(server.server_address, timeout=2)


def request(server: Pool, wire: bytes, *, half_close: bool = False) -> bytes:
    with connect(server) as sock:
        sock.sendall(wire)
        if half_close:
            sock.shutdown(socket.SHUT_WR)
        return receive(sock)


def drip(server: Pool, prefix: bytes, chunks: list[bytes], delay: float) -> tuple[bytes, float]:
    with connect(server) as sock:
        stopped = threading.Event()
        def writer() -> None:
            for chunk in chunks:
                if stopped.wait(delay):
                    return
                try:
                    sock.sendall(chunk)
                except OSError:
                    return
        started = time.monotonic()
        sock.sendall(prefix)
        thread = threading.Thread(target=writer)
        thread.start()
        try:
            result = receive(sock)
        finally:
            stopped.set()
            thread.join(2)
        return result, time.monotonic() - started


def main() -> None:
    server = Pool()
    runner = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
    runner.start()
    try:
        healthy = request(server, b"GET / HTTP/1.1\r\nHost: localhost\r\n\r\n")
        assert status(healthy) == 200
        assert json.loads(healthy.split(b"\r\n\r\n", 1)[1])["socket_timeout"] == 2.0

        # Sending a byte often enough to defeat an idle timeout must not extend
        # either absolute deadline or monopolize workers indefinitely.
        response, elapsed = drip(server, b"GET / HTTP/1.1\r\nX-Slow: ", [b"a"] * 60, 0.03)
        assert status(response) == 408 and elapsed < 1.0, (elapsed, response)
        response, elapsed = drip(
            server, b"POST / HTTP/1.1\r\nContent-Length: 40\r\n\r\nx", [b"x"] * 39, 0.03,
        )
        assert status(response) == 408 and elapsed < 1.0, (elapsed, response)
        response, elapsed = drip(server, b"G", [b"E"] * 60, 0.03)
        assert status(response) == 408 and elapsed < 1.0

        for framing, expected in (
            (b"Content-Length: 1\r\nContent-Length: 1", 400),
            (b"Content-Length: -1", 400), (b"Content-Length: +1", 400),
            (b"Content-Length: 1,1", 400), (b"Content-Length: 1e2", 400),
            (b"Content-Length: 999999999", 413),
            (b"Content-Length: " + b"9" * 100, 400),
            (b"Transfer-Encoding: chunked\r\nContent-Length: 1", 400),
            (b"Transfer-Encoding: identity", 400),
            (b"Transfer-Encoding: ", 400),
        ):
            response = request(server, b"POST / HTTP/1.1\r\n" + framing + b"\r\n\r\n")
            assert status(response) == expected, response
        too_large = request(server, b"GET / HTTP/1.1\r\nX-Private: " + b"PRIVATE" * 200 + b"\r\n\r\n")
        assert status(too_large) == 431
        premature_end = request(server, b"POST / HTTP/1.1\r\nContent-Length: 10\r\n\r\nxx", half_close=True)
        assert status(premature_end) == 400
        expected_reject = request(server, b"POST / HTTP/1.1\r\nContent-Length: 9999\r\nExpect: 100-continue\r\n\r\n")
        assert status(expected_reject) == 413 and b"100 Continue" not in expected_reject

        # Legitimate upload gets a separate bounded budget and size allowance;
        # claiming multipart on an ordinary route does not obtain that allowance.
        upload_headers = (
            b"POST /api/products/import HTTP/1.1\r\nContent-Type: multipart/form-data; boundary=test\r\n"
            b"Content-Length: 100\r\n\r\n"
        )
        upload, elapsed = drip(server, upload_headers, [b"x" * 20] * 5, 0.08)
        assert status(upload) == 200 and elapsed > Handler.body_read_timeout
        assert json.loads(upload.split(b"\r\n\r\n", 1)[1])["received"] == 100
        wrong_route = request(server, upload_headers.replace(b"/api/products/import", b"/ordinary"))
        assert status(wrong_route) == 413
        for wire in (
            b"GET /slow-response HTTP/1.1\r\n\r\n",
            b"POST /slow-response HTTP/1.1\r\nContent-Length: 2\r\n\r\n{}",
        ):
            response = request(server, wire)
            assert status(response) == 200
            assert json.loads(response.split(b"\r\n\r\n", 1)[1])["socket_timeout"] == 2.0

        # Fill both worker slots with incomplete headers. Excess requests are
        # rejected promptly instead of blocking the HTTPServer accept loop.
        with contextlib.ExitStack() as stack:
            clients = [stack.enter_context(connect(server)) for _ in range(2)]
            for client in clients:
                client.sendall(b"GET / HTTP/1.1\r\nX-Slow:")
            deadline = time.monotonic() + 0.2
            while server.slots._value and time.monotonic() < deadline:
                time.sleep(0.005)
            started = time.monotonic()
            overloaded = request(server, b"GET / HTTP/1.1\r\n\r\n")
            assert status(overloaded) == 503
            assert b"Retry-After: 2\r\n" in overloaded
            assert time.monotonic() - started < 0.2
            for client in clients:
                assert status(receive(client)) == 408
        assert status(request(server, b"GET / HTTP/1.1\r\n\r\n")) == 200
    finally:
        server.shutdown()
        runner.join(2)
        server.server_close()
    print("http limits test passed")


if __name__ == "__main__":
    main()
