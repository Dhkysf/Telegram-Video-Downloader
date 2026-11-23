"""Tiny local HTTP proxy that only lets the headless browser reach the public internet.

Playwright's request routing does not see redirects, and the browser resolves
DNS itself, so a page could still bounce the browser to 192.168.x.x or
localhost. Sending ALL browser traffic through this proxy closes that gap:
every upstream connection is opened inside :func:`public_network_only`, so the
socket-level guard checks the real IP address of each connection (redirects,
iframes, WebSockets and DNS tricks included).

The proxy listens on 127.0.0.1 on a random port, lives only for one browser
session, and never raises to its caller.
"""

from __future__ import annotations

import logging
import selectors
import socket
import threading
from typing import Optional
from urllib.parse import urlsplit

from .common import BlockedAddressError, public_network_only

logger = logging.getLogger(__name__)

LISTEN_HOST: str = "127.0.0.1"
HEADER_LIMIT: int = 64 * 1024
CLIENT_TIMEOUT_SECONDS: float = 30.0
UPSTREAM_CONNECT_TIMEOUT: float = 15.0
IDLE_TIMEOUT_SECONDS: float = 120.0
RELAY_CHUNK: int = 64 * 1024
MAX_TUNNELS: int = 64
DROPPED_HEADERS: frozenset[str] = frozenset({"proxy-connection", "proxy-authorization", "connection", "keep-alive"})
BLOCKED_RESPONSE: bytes = b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
BAD_GATEWAY_RESPONSE: bytes = b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
BAD_REQUEST_RESPONSE: bytes = b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
BUSY_RESPONSE: bytes = b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"


class GuardProxy:
    """A per-session forward proxy (HTTP + CONNECT) with the public-network guard."""

    def __init__(self, max_tunnels: int = MAX_TUNNELS) -> None:
        """Create the listening socket (not yet accepting)."""
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.bind((LISTEN_HOST, 0))
        self._server.listen(128)
        self._closed = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._slots = threading.BoundedSemaphore(max_tunnels)
        self._lock = threading.Lock()
        self._active_sockets: set[socket.socket] = set()
        self.blocked = 0  # Number of connections refused (for logs/tests).

    @property
    def url(self) -> str:
        """Proxy address for the browser, e.g. ``http://127.0.0.1:54321``."""
        host, port = self._server.getsockname()[:2]
        return f"http://{host}:{port}"

    def __enter__(self) -> "GuardProxy":
        """Start accepting connections in a background thread."""
        self._thread = threading.Thread(target=self._accept_loop, name="guard-proxy", daemon=True)
        self._thread.start()
        return self

    def _register_socket(self, sock: socket.socket) -> bool:
        with self._lock:
            if self._closed.is_set():
                return False
            self._active_sockets.add(sock)
            return True

    def __exit__(self, *_exc: object) -> None:
        """Stop the proxy and close any active client/upstream sockets."""
        self._closed.set()
        try:
            self._server.close()
        except OSError:
            pass
        with self._lock:
            sockets = list(self._active_sockets)
            self._active_sockets.clear()
        for sock in sockets:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass

    def _accept_loop(self) -> None:
        while not self._closed.is_set():
            try:
                client, _addr = self._server.accept()
            except OSError:
                return  # Server socket closed.
            if not self._slots.acquire(blocking=False):
                try:
                    client.settimeout(1.0)
                    client.sendall(BUSY_RESPONSE)
                except OSError:
                    pass
                try:
                    client.close()
                except OSError:
                    pass
                continue
            try:
                threading.Thread(target=self._handle, args=(client,), name="guard-proxy-conn", daemon=True).start()
            except Exception:
                self._slots.release()
                try:
                    client.close()
                except OSError:
                    pass

    def _handle(self, client: socket.socket) -> None:
        upstream: Optional[socket.socket] = None
        try:
            if not self._register_socket(client):
                return
            client.settimeout(CLIENT_TIMEOUT_SECONDS)
            head, rest = _read_head(client)
            if head is None:
                client.sendall(BAD_REQUEST_RESPONSE)
                return
            request_line, _sep, header_block = head.partition(b"\r\n")
            parts = request_line.decode("latin-1").split()
            if len(parts) != 3:
                client.sendall(BAD_REQUEST_RESPONSE)
                return
            method, target, version = parts
            if method.upper() == "CONNECT":
                host, _colon, port_text = target.rpartition(":")
                host = host.strip("[]")
                upstream = self._connect(client, host, int(port_text or 443))
                if upstream is None or not self._register_socket(upstream):
                    return
                client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                if rest:
                    upstream.sendall(rest)
            else:
                url = urlsplit(target)
                if url.scheme.lower() != "http" or not url.hostname:
                    client.sendall(BAD_REQUEST_RESPONSE)
                    return
                upstream = self._connect(client, url.hostname, url.port or 80)
                if upstream is None or not self._register_socket(upstream):
                    return
                path = url.path or "/"
                if url.query:
                    path += "?" + url.query
                headers = [
                    line for line in header_block.split(b"\r\n")
                    if line and line.split(b":", 1)[0].strip().lower().decode("latin-1") not in DROPPED_HEADERS
                ]
                # Connection: close keeps one upstream per client connection, so a
                # reused browser connection can never reach a different host unchecked.
                upstream.sendall(
                    f"{method} {path} {version}\r\n".encode("latin-1")
                    + b"\r\n".join(headers)
                    + b"\r\nConnection: close\r\n\r\n"
                    + rest
                )
            _relay(client, upstream)
        except (OSError, ValueError):
            logger.debug("Proxy connection error", exc_info=True)
        finally:
            with self._lock:
                self._active_sockets.discard(client)
                if upstream is not None:
                    self._active_sockets.discard(upstream)
            for sock in (client, upstream):
                if sock is not None:
                    try:
                        sock.close()
                    except OSError:
                        pass
            self._slots.release()

    def _connect(self, client: socket.socket, host: str, port: int) -> Optional[socket.socket]:
        """Open an upstream connection under the guard; answer the client on failure."""
        try:
            with public_network_only():
                return socket.create_connection((host, port), timeout=UPSTREAM_CONNECT_TIMEOUT)
        except BlockedAddressError:
            self.blocked += 1
            logger.info("Browser proxy blocked a connection to a non-public address (%s)", host)
            client.sendall(BLOCKED_RESPONSE)
        except OSError:
            client.sendall(BAD_GATEWAY_RESPONSE)
        return None


def _read_head(client: socket.socket) -> tuple[Optional[bytes], bytes]:
    """Read the request head. Returns (head without final CRLFCRLF, bytes after it)."""
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = client.recv(8192)
        if not chunk:
            return None, b""
        data += chunk
        if len(data) > HEADER_LIMIT:
            return None, b""
    head, _sep, rest = data.partition(b"\r\n\r\n")
    return head, rest


def _relay(first: socket.socket, second: socket.socket) -> None:
    """Copy bytes both ways until either side closes or the link goes idle."""
    first.settimeout(None)
    second.settimeout(None)
    peers = {first: second, second: first}
    with selectors.DefaultSelector() as selector:
        selector.register(first, selectors.EVENT_READ)
        selector.register(second, selectors.EVENT_READ)
        while True:
            events = selector.select(timeout=IDLE_TIMEOUT_SECONDS)
            if not events:
                return
            for key, _mask in events:
                source = key.fileobj
                try:
                    data = source.recv(RELAY_CHUNK)  # type: ignore[union-attr]
                except OSError:
                    return
                if not data:
                    return
                try:
                    peers[source].sendall(data)  # type: ignore[index]
                except OSError:
                    return
