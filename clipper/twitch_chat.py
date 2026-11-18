"""Read a public Twitch chat anonymously over IRC (read-only, no account, no token).

Twitch allows anonymous read-only connections with a `justinfanNNNN` nick.
This client never sends chat messages.
"""
from __future__ import annotations

import logging
import random
import socket
import ssl
import threading
import time
from typing import Callable

log = logging.getLogger(__name__)

HOST, PORT = "irc.chat.twitch.tv", 6697

OnMessage = Callable[[float, str, str], None]  # (unix_ts, user, text)


def parse_privmsg(line: str) -> tuple[str, str] | None:
    """Return (user, text) for a PRIVMSG line, else None. Handles optional @tags."""
    if line.startswith("@"):
        _, _, line = line.partition(" ")
    if " PRIVMSG #" not in line or not line.startswith(":"):
        return None
    prefix, _, rest = line[1:].partition(" PRIVMSG #")
    user = prefix.split("!", 1)[0]
    _, _, text = rest.partition(" :")
    if text.startswith("\x01ACTION ") and text.endswith("\x01"):
        text = text[8:-1]
    return user, text


class ChatReader(threading.Thread):
    def __init__(self, channel: str, on_message: OnMessage, stop: threading.Event):
        super().__init__(daemon=True, name=f"chat-{channel}")
        self.channel = channel.lower().lstrip("#")
        self.on_message = on_message
        self.stop_event = stop
        self.connected = threading.Event()

    def run(self) -> None:
        backoff = 2.0
        while not self.stop_event.is_set():
            try:
                self._session()
                backoff = 2.0
            except (OSError, ssl.SSLError) as exc:
                log.warning("chat %s disconnected: %s; retry in %.0fs", self.channel, exc, backoff)
            self.connected.clear()
            if self.stop_event.wait(backoff):
                break
            backoff = min(backoff * 2, 60)

    def _session(self) -> None:
        ctx = ssl.create_default_context()
        raw = socket.create_connection((HOST, PORT), timeout=30)
        with ctx.wrap_socket(raw, server_hostname=HOST) as sock:
            sock.settimeout(5)
            nick = f"justinfan{random.randint(10000, 99999)}"
            sock.sendall(f"NICK {nick}\r\nJOIN #{self.channel}\r\n".encode())
            buf = b""
            last_rx = time.time()
            while not self.stop_event.is_set():
                try:
                    data = sock.recv(65536)
                except socket.timeout:
                    if time.time() - last_rx > 360:
                        raise OSError("no data for 6 minutes")
                    continue
                if not data:
                    raise OSError("server closed connection")
                last_rx = time.time()
                buf += data
                *lines, buf = buf.split(b"\r\n")
                for raw_line in lines:
                    line = raw_line.decode("utf-8", "replace")
                    if line.startswith("PING"):
                        sock.sendall(line.replace("PING", "PONG", 1).encode() + b"\r\n")
                        continue
                    if " 366 " in line or " JOIN #" in line:
                        self.connected.set()
                    parsed = parse_privmsg(line)
                    if parsed:
                        try:
                            self.on_message(last_rx, parsed[0], parsed[1])
                        except Exception:  # never let a callback kill the reader
                            log.exception("chat callback failed")
