"""docreader `read_document(file_url=…)` SSRF/LFI guard.

The `file_url` form must never reach file:// (LFI), loopback, link-local,
private (RFC1918), reserved, or cloud-metadata targets; redirect hops are
re-validated; unresolvable hosts fail closed; an optional host allowlist
pins the reachable hosts. Local files go through the broker-scoped `path`
form, never through a URL.

Runs on the host python3 (run-tests.sh unit tier, `unittest discover`) —
the `mcp` import is stubbed and DNS + transport are mocked, so no MCP
deps and no real network are needed. Mirrors the PHP contract in
`control-plane/app/Support/SafeHttp.php` (`MSecSafeFetch1Test`).
"""
from __future__ import annotations

import os
import socket
import sys
import types
import unittest
from types import SimpleNamespace
from unittest import mock

# --- stub `mcp.server.mcpserver` so `import server` needs no MCP deps -------
#
# The stubbed `mcp.server` must be a PACKAGE, not a plain module. It used to be
# a module and that was invisible while the import was one level deep
# (`mcp.server.fastmcp` was set directly in sys.modules); migrating to
# `mcp.server.mcpserver` made Python resolve the middle name itself, and it
# answered "'mcp.server' is not a package" — a stub failing on the shape it had
# always had, on the day the real import moved.
if "mcp.server.mcpserver" not in sys.modules:
    class _MCPServer:
        def __init__(self, *args, **kwargs):
            pass

        def tool(self, *args, **kwargs):
            def deco(fn):
                return fn

            return deco

        def run(self, *args, **kwargs):
            pass

    _mcpserver = types.ModuleType("mcp.server.mcpserver")
    _mcpserver.MCPServer = _MCPServer
    _mcp_server = types.ModuleType("mcp.server")
    _mcp_server.__path__ = []          # un pacchetto, o il nome di mezzo non risolve
    _mcp = types.ModuleType("mcp")
    _mcp.__path__ = []
    sys.modules.setdefault("mcp", _mcp)
    sys.modules.setdefault("mcp.server", _mcp_server)
    sys.modules["mcp.server.mcpserver"] = _mcpserver

import server  # noqa: E402

_PUBLIC_IP = "93.184.216.34"  # example.com — documentation/public range


def _addrinfo(ip: str):
    """One getaddrinfo()-shaped row for `ip`."""
    family = socket.AF_INET6 if ":" in ip else socket.AF_INET
    sockaddr = (ip, 80, 0, 0) if family == socket.AF_INET6 else (ip, 80)
    return [(family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", sockaddr)]


def _resolve_to(ip: str):
    return mock.patch("socket.getaddrinfo", return_value=_addrinfo(ip))


class ValidateFetchUrlRejectsTest(unittest.TestCase):
    """Every hostile shape is refused with ValueError (fail closed)."""

    def test_file_scheme_rejected(self):
        with self.assertRaises(ValueError):
            server._validate_fetch_url("file:///etc/passwd")

    def test_ftp_scheme_rejected(self):
        with self.assertRaises(ValueError):
            server._validate_fetch_url("ftp://example.com/doc.pdf")

    def test_scheme_case_does_not_bypass(self):
        with self.assertRaises(ValueError):
            server._validate_fetch_url("FILE:///etc/passwd")

    def test_no_host_rejected(self):
        with self.assertRaises(ValueError):
            server._validate_fetch_url("http:///nohost")

    def test_unparseable_rejected(self):
        with self.assertRaises(ValueError):
            server._validate_fetch_url("http://[::1oops/")

    def test_metadata_ip_rejected(self):
        with self.assertRaises(ValueError):
            server._validate_fetch_url("http://169.254.169.254/latest/meta-data/")

    def test_rfc1918_ip_rejected(self):
        for ip in ("10.0.0.8", "172.16.3.4", "192.168.1.10"):
            with self.subTest(ip=ip), self.assertRaises(ValueError):
                server._validate_fetch_url(f"http://{ip}/x")

    def test_loopback_ip_rejected(self):
        with self.assertRaises(ValueError):
            server._validate_fetch_url("http://127.0.0.1:8000/x")

    def test_ipv6_loopback_rejected(self):
        with self.assertRaises(ValueError):
            server._validate_fetch_url("http://[::1]/x")

    def test_localhost_hostname_rejected(self):
        with _resolve_to("127.0.0.1"), self.assertRaises(ValueError):
            server._validate_fetch_url("http://localhost/x")

    def test_hostname_resolving_private_rejected(self):
        with _resolve_to("192.168.1.10"), self.assertRaises(ValueError):
            server._validate_fetch_url("http://internal.example.com/x")

    def test_unresolvable_host_fails_closed(self):
        with mock.patch(
            "socket.getaddrinfo", side_effect=socket.gaierror("NXDOMAIN")
        ), self.assertRaises(ValueError):
            server._validate_fetch_url("https://no-such-host.invalid/doc.pdf")

    def test_allowlist_blocks_other_hosts(self):
        env = {"CERASE_FETCH_ALLOWED_HOSTS": "docs.example.com"}
        with mock.patch.dict(os.environ, env), _resolve_to(_PUBLIC_IP):
            with self.assertRaises(ValueError):
                server._validate_fetch_url("https://evil.example.net/doc.pdf")
            # allowlisted host still passes
            server._validate_fetch_url("https://docs.example.com/doc.pdf")


class ValidateFetchUrlAllowsTest(unittest.TestCase):
    def test_public_https_url_passes(self):
        with _resolve_to(_PUBLIC_IP):
            server._validate_fetch_url("https://docs.example.com/doc.pdf")

    def test_public_http_url_passes(self):
        with _resolve_to(_PUBLIC_IP):
            server._validate_fetch_url("http://docs.example.com/doc.pdf")


class SafeRedirectTest(unittest.TestCase):
    """A safe host 302'ing to a forbidden target is refused mid-flight."""

    def test_redirect_to_metadata_rejected(self):
        handler = server._SafeRedirectHandler()
        req = mock.MagicMock()
        with self.assertRaises(ValueError):
            handler.redirect_request(
                req, None, 302, "Found", {}, "http://169.254.169.254/latest/"
            )

    def test_redirect_to_file_rejected(self):
        handler = server._SafeRedirectHandler()
        req = mock.MagicMock()
        with self.assertRaises(ValueError):
            handler.redirect_request(req, None, 302, "Found", {}, "file:///etc/passwd")


class SafeFetchTransportTest(unittest.TestCase):
    """_safe_fetch: guard first, bounded read, mocked transport."""

    def _fake_opener(self, body: bytes):
        resp = mock.MagicMock()
        resp.read.side_effect = lambda n=-1: body if n < 0 else body[:n]
        resp.__enter__ = mock.Mock(return_value=resp)
        resp.__exit__ = mock.Mock(return_value=False)
        opener = mock.MagicMock()
        opener.open.return_value = resp
        return opener

    def test_fetches_validated_url(self):
        opener = self._fake_opener(b"%PDF-1.7 hello")
        with _resolve_to(_PUBLIC_IP), mock.patch(
            "urllib.request.build_opener", return_value=opener
        ):
            raw = server._safe_fetch("https://docs.example.com/doc.pdf")
        self.assertEqual(raw, b"%PDF-1.7 hello")
        opener.open.assert_called_once()

    def test_forbidden_url_never_reaches_transport(self):
        opener = self._fake_opener(b"nope")
        with mock.patch("urllib.request.build_opener", return_value=opener):
            with self.assertRaises(ValueError):
                server._safe_fetch("file:///etc/passwd")
        opener.open.assert_not_called()

    def test_oversized_body_rejected(self):
        opener = self._fake_opener(b"x" * 32)
        with _resolve_to(_PUBLIC_IP), mock.patch(
            "urllib.request.build_opener", return_value=opener
        ), mock.patch.object(server, "_MAX_FETCH_BYTES", 16):
            with self.assertRaises(ValueError):
                server._safe_fetch("https://docs.example.com/doc.pdf")


class ReadDocumentSinkTest(unittest.TestCase):
    """The tool-level sink routes file_url through the guard."""

    def test_file_url_lfi_rejected(self):
        with self.assertRaises(ValueError):
            server.read_document(file_url="file:///etc/passwd")

    def test_metadata_url_rejected(self):
        with self.assertRaises(ValueError):
            server.read_document(file_url="http://169.254.169.254/latest/meta-data/")

    def test_rfc1918_url_rejected(self):
        with self.assertRaises(ValueError):
            server.read_document(file_url="http://10.0.0.8/doc.pdf")

    def test_public_https_url_converts(self):
        opener = mock.MagicMock()
        resp = mock.MagicMock()
        resp.read.side_effect = lambda n=-1: b"hello"
        resp.__enter__ = mock.Mock(return_value=resp)
        resp.__exit__ = mock.Mock(return_value=False)
        opener.open.return_value = resp

        converter = mock.MagicMock()
        converter.convert.return_value = SimpleNamespace(text_content="hello text")

        with _resolve_to(_PUBLIC_IP), mock.patch(
            "urllib.request.build_opener", return_value=opener
        ), mock.patch.object(server, "_converter", return_value=converter):
            out = server.read_document(file_url="https://docs.example.com/doc.pdf")

        self.assertEqual(out["text"], "hello text")
        self.assertEqual(out["format"], "pdf")


if __name__ == "__main__":
    unittest.main()
