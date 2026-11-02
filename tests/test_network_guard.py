"""The default suite cannot accidentally dispatch to remote or local providers."""

import socket

import pytest


def test_remote_dns_is_denied():
    with pytest.raises(pytest.fail.Exception, match="network DNS"):
        socket.getaddrinfo("unmocked-provider.invalid", 443)


def test_unmarked_loopback_connection_is_denied():
    with socket.socket() as connection:
        with pytest.raises(pytest.fail.Exception, match="network connection"):
            connection.connect(("127.0.0.1", 11434))


def test_framework_socketpair_remains_local_and_available():
    one, two = socket.socketpair()
    try:
        one.sendall(b"local")
        assert two.recv(5) == b"local"
    finally:
        one.close()
        two.close()
