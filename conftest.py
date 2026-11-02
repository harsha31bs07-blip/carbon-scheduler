# Presence of this file makes pytest put the repo root on sys.path so `data` and `shared` import from anywhere.

import pytest
import ipaddress
import socket
import threading

from shared import availability


@pytest.fixture(autouse=True)
def deny_network_by_default(monkeypatch, request):
    """Deny outbound DNS/TCP; explicit loopback tests may contact local servers."""
    allow_loopback = request.node.get_closest_marker("loopback") is not None
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    original_getaddrinfo = socket.getaddrinfo
    original_socketpair = socket.socketpair
    internal = threading.local()

    def local(host):
        if host == "localhost":
            return True
        try:
            return ipaddress.ip_address(host).is_loopback
        except (ValueError, TypeError):
            return False

    def check(sock, address):
        if sock.family in {socket.AF_INET, socket.AF_INET6}:
            if not (allow_loopback or getattr(internal, "socketpair", False)) or not local(address[0]):
                pytest.fail("Unexpected network connection; mock it or mark an explicit loopback test")

    def connect(sock, address):
        check(sock, address)
        return original_connect(sock, address)

    def connect_ex(sock, address):
        check(sock, address)
        return original_connect_ex(sock, address)

    def getaddrinfo(host, *args, **kwargs):
        if host is not None and not local(host):
            pytest.fail("Unexpected network DNS lookup; tests must use retained fixtures")
        return original_getaddrinfo(host, *args, **kwargs)

    def socketpair(*args, **kwargs):
        # Windows asyncio implements its private wakeup pipe using a temporary
        # loopback pair. Permit only connections made while constructing that pair.
        previous = getattr(internal, "socketpair", False)
        internal.socketpair = True
        try:
            return original_socketpair(*args, **kwargs)
        finally:
            internal.socketpair = previous

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    monkeypatch.setattr(socket, "socketpair", socketpair)


@pytest.fixture(autouse=True)
def offline_by_default(monkeypatch):
    """Run every test in offline mode so results don't depend on the developer's .env."""
    monkeypatch.setattr(availability, "gemini_status", lambda: (False, "disabled in tests"))
    monkeypatch.setattr(availability, "nvidia_status", lambda: (False, "disabled in tests"))


@pytest.fixture
def hybrid(monkeypatch):
    """Call hybrid(gemini=..., nvidia=...) to switch cloud providers on for one test."""

    def enable(gemini: bool = True, nvidia: bool = False) -> None:
        monkeypatch.setattr(availability, "gemini_status", lambda: (gemini, "enabled in test"))
        monkeypatch.setattr(availability, "nvidia_status", lambda: (nvidia, "enabled in test"))

    return enable
