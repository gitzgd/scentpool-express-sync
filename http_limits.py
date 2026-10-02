"""Bounded HTTP request reading for the existing stdlib server.

Only receiving headers/body has an absolute deadline. Provider work, PDF
generation and response streaming are not charged to that receive budget.
No timers or per-request background threads are created.
"""

from __future__ import annotations

import io
import json
import re
import socket
import time
from typing import Any
from urllib.parse import urlsplit


class HttpRequestError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class _DeadlineSocketReader(io.RawIOBase):
    def __init__(self, handler: Any) -> None:
        super().__init__()
        self.handler = handler

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Any) -> int:
        handler = self.handler
        remaining = handler._receive_deadline - time.monotonic()
        if remaining <= 0:
            raise HttpRequestError(408, "接收请求超时，本次请求未完成，请检查网络后重试。")
        connection = handler.connection
        connection.settimeout(remaining)
        try:
            return connection.recv_into(buffer)
        except (socket.timeout, TimeoutError):
            raise HttpRequestError(408, "接收请求超时，本次请求未完成，请检查网络后重试。") from None
        finally:
            # A 600-second provider/merge computation is not a body read. Writes
            # have a separate idle timeout and never inherit a nearly spent
            # header/body deadline.
            connection.settimeout(handler.response_write_timeout)


class _RequestReader(io.BufferedReader):
    def __init__(self, handler: Any) -> None:
        super().__init__(_DeadlineSocketReader(handler), buffer_size=8192)
        self.handler = handler

    def readline(self, size: int = -1) -> bytes:
        handler = self.handler
        if not handler._reading_headers:
            return super().readline(size)
        if time.monotonic() >= handler._receive_deadline:
            raise HttpRequestError(408, "接收请求头超时，请检查网络后重试。")
        remaining = handler.max_header_bytes - handler._header_bytes_read
        capped = remaining + 1 if size < 0 else min(size, remaining + 1)
        line = super().readline(capped)
        handler._header_bytes_read += len(line)
        if handler._header_bytes_read > handler.max_header_bytes:
            raise HttpRequestError(431, "请求头过大，请重新登录或缩小请求后重试。")
        return line


class RequestReadLimitsMixin:
    """Place before BaseHTTPRequestHandler; use read_request_body for all bodies.

    The application's route exception handler must translate HttpRequestError
    to its JSON error response before its generic Exception catch.
    """

    header_read_timeout = 10.0
    body_read_timeout = 30.0
    upload_read_timeout = 120.0
    response_write_timeout = 60.0
    max_header_bytes = 64 * 1024
    max_normal_body_bytes = 1024 * 1024
    max_upload_body_bytes = 20 * 1024 * 1024
    upload_body_paths = frozenset({"/api/products/import", "/api/admin/restore-db"})

    def setup(self) -> None:
        super().setup()
        self.rfile.close()
        self.rfile = _RequestReader(self)
        self.connection.settimeout(self.response_write_timeout)

    def handle_one_request(self) -> None:
        self._reading_headers = True
        self._header_bytes_read = 0
        self._receive_deadline = time.monotonic() + self.header_read_timeout
        self._request_body_length = 0
        self._request_body_consumed = True
        self.requestline = ""
        self.request_version = "HTTP/1.0"
        self.command = None
        try:
            super().handle_one_request()
        except HttpRequestError as exc:
            self.close_connection = True
            self._send_read_limit_error(exc)
        finally:
            if not self._request_body_consumed:
                # Never interpret an unread body as the next keep-alive request.
                self.close_connection = True

    def _validate_request_framing(self) -> None:
        if self.headers.get_all("Transfer-Encoding"):
            raise HttpRequestError(400, "不支持分块或其他传输编码，请重新提交标准请求。")
        lengths = self.headers.get_all("Content-Length", [])
        if len(lengths) > 1:
            raise HttpRequestError(400, "请求长度重复，请重新提交。")
        text = lengths[0] if lengths else "0"
        if not re.fullmatch(r"[0-9]{1,9}", text):
            raise HttpRequestError(400, "请求长度格式不正确，请重新提交。")
        length = int(text)
        is_upload = (
            self.command == "POST"
            and urlsplit(self.path).path in self.upload_body_paths
            and self.headers.get("Content-Type", "").lower().startswith("multipart/form-data")
        )
        limit = self.max_upload_body_bytes if is_upload else self.max_normal_body_bytes
        if length > limit:
            raise HttpRequestError(413, "请求内容超过安全大小上限，请减少内容后重试。")
        self._request_body_length = length
        self._request_body_consumed = length == 0
        self._is_upload_body = is_upload

    def handle_expect_100(self) -> bool:
        self._validate_request_framing()
        return super().handle_expect_100()

    def parse_request(self) -> bool:
        parsed = super().parse_request()
        if not parsed:
            return False
        self._validate_request_framing()
        self._reading_headers = False
        allowance = self.upload_read_timeout if self._is_upload_body else self.body_read_timeout
        self._receive_deadline = time.monotonic() + allowance
        return True

    def read_request_body(self, max_bytes: int) -> bytes:
        length = self._request_body_length
        if length > max_bytes:
            self.close_connection = True
            raise HttpRequestError(413, "请求内容超过安全大小上限，请减少内容后重试。")
        if self._request_body_consumed:
            if length == 0:
                return b""
            raise HttpRequestError(400, "请求内容已读取，不能重复读取。")
        if time.monotonic() >= self._receive_deadline:
            self.close_connection = True
            raise HttpRequestError(408, "接收请求内容超时，请检查网络后重试。")
        try:
            body = self.rfile.read(length)
        except HttpRequestError:
            self.close_connection = True
            raise
        if len(body) != length:
            self.close_connection = True
            raise HttpRequestError(400, "请求内容未完整收到，本次操作未提交，请检查网络后重试。")
        self._request_body_consumed = True
        return body

    def _send_read_limit_error(self, error: HttpRequestError) -> None:
        body = json.dumps({"error": error.message}, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(error.status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
        except OSError:
            pass  # Peer disconnected; nothing remains queued or partially executed.


def reject_overloaded_request(request: socket.socket, retry_after: int = 2) -> None:
    """Reject before dispatch without blocking the accept loop on a full pool."""
    retry_after = max(1, min(int(retry_after), 60))
    body = json.dumps(
        {"error": "网站当前请求较多，本次请求未开始处理，请稍后重试。", "retryable": True},
        ensure_ascii=False,
    ).encode("utf-8")
    headers = (
        "HTTP/1.1 503 Service Unavailable\r\n"
        "Content-Type: application/json; charset=utf-8\r\n"
        f"Content-Length: {len(body)}\r\nRetry-After: {retry_after}\r\n"
        "Cache-Control: no-store\r\nConnection: close\r\n\r\n"
    ).encode("ascii")
    try:
        request.settimeout(0.05)
        request.sendall(headers + body)
    except OSError:
        pass
    finally:
        try:
            request.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        request.close()
