#!/usr/bin/env python3
"""Cerase Docreader MCP — first-party document→text extraction.

TOOLS-2: Cerase-owned MCP (was external kiso-docreader). Extracts
plain text / markdown out of office + web document formats
(PDF/DOCX/XLSX/PPTX/HTML/MD/RTF/ODT/EPUB/CSV) via markitdown. Owned for
standardization + version-pinning even though it makes NO LLM call — it
is therefore UNBILLED (no tool-model token charge, no flat fee).

Tool:
  - read_document(file_url?, file_base64?, filename?) → {text, format}

Exactly one of file_url / file_base64. `filename` (or the URL suffix)
provides the extension hint markitdown uses to pick a converter.

No agent_id needed — there is no upstream LLM call to attribute.
"""
from __future__ import annotations

import base64
import ipaddress
import logging
import os
import socket
import sys
import tempfile
import urllib.request
from typing import Any
from urllib.parse import urlparse

from mcp.server.fastmcp import FastMCP

# MCP stdio transport uses stdout as the JSON-RPC channel — any log on stdout
# corrupts the protocol. Own the stderr invariant explicitly (don't depend on
# the mcp SDK's default handler, which could change across versions) and quiet
# httpx so backend request URLs don't leak into the logs. (Shared stdio-logging
# bootstrap across the first-party MCP connectors — search / media / docreader;
# docreader uses urllib not httpx, so that line is a harmless parity no-op here.)
logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)

mcp = FastMCP("cerase-docreader")

_md = None


def _safe_local_path(path: str) -> str:
    """Resolve a workspace path, refusing anything that escapes the
    shared workspace root (path-traversal guard — the agent supplies
    `path`, so a crafted `../../etc/passwd` must not read host files).
    """
    root = os.path.realpath(os.environ.get("CERASE_TOOL_WORKSPACE_ROOT", "/workspace"))
    resolved = os.path.realpath(path)
    if resolved != root and not resolved.startswith(root + os.sep):
        raise ValueError("path escapes the workspace root")
    return resolved


# Cap on remote document downloads, in bytes.
_MAX_FETCH_BYTES = int(os.environ.get("CERASE_FETCH_MAX_BYTES", 50 * 1024 * 1024))


def _validate_fetch_url(url: str) -> str:
    """SSRF/LFI guard for a caller-supplied fetch URL.

    Only http(s) URLs whose host resolves to a public address may be
    fetched server-side: file:// / ftp:// / any other scheme is refused,
    as is any host that is — or resolves to — a loopback, link-local,
    private (RFC1918), reserved or otherwise non-public address (kills
    the cloud-metadata classic 169.254.169.254 and pivots into the
    compose-internal network). `CERASE_FETCH_ALLOWED_HOSTS` (comma-
    separated, exact hostnames, case-insensitive) optionally pins the
    reachable hosts. Fail-closed: unparseable or unresolvable → raise.
    Mirrors the PHP contract in control-plane `App\\Support\\SafeHttp`.
    """
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").strip().lower()
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f"unparseable URL — refusing to fetch: {exc}") from exc
    if parsed.scheme not in ("http", "https"):
        raise ValueError(
            f"URL scheme {parsed.scheme!r} refused — only http/https may be fetched"
        )
    if not host:
        raise ValueError("URL has no host — refusing to fetch")

    allowlist = {
        h.strip().lower()
        for h in os.environ.get("CERASE_FETCH_ALLOWED_HOSTS", "").split(",")
        if h.strip()
    }
    if allowlist and host not in allowlist:
        raise ValueError(f"host {host!r} is not on the fetch allowlist")

    # Name-based fast fail — resolver-independent.
    if host == "localhost" or host.endswith(".localhost"):
        raise ValueError("refusing to fetch localhost")

    try:
        infos = socket.getaddrinfo(
            host, port or (443 if parsed.scheme == "https" else 80),
            proto=socket.IPPROTO_TCP,
        )
    except (socket.gaierror, OSError) as exc:
        raise ValueError(
            f"could not resolve host {host!r} — refusing to fetch (fail-closed)"
        ) from exc
    if not infos:
        raise ValueError(
            f"could not resolve host {host!r} — refusing to fetch (fail-closed)"
        )
    for info in infos:
        addr = ipaddress.ip_address(info[4][0].split("%")[0])
        if (
            str(addr) == "169.254.169.254"  # cloud metadata — named explicitly
            or addr.is_loopback
            or addr.is_link_local
            or addr.is_private
            or addr.is_reserved
            or addr.is_multicast
            or addr.is_unspecified
            or not addr.is_global  # CGNAT, TEST-NETs, anything else non-public
        ):
            raise ValueError(
                f"host {host!r} points at a private/reserved address ({addr}) — "
                "server-side fetch refused"
            )
    return url


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Re-validate every redirect hop — a safe host 302'ing to file:// or
    http://169.254.169.254 must be refused mid-flight."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        _validate_fetch_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _safe_fetch(url: str, timeout: int = 60) -> bytes:
    """Fetch a caller-supplied URL defensively: validate scheme + resolved
    host first, re-validate redirect hops, bound the read size and the
    connect time."""
    _validate_fetch_url(url)
    opener = urllib.request.build_opener(_SafeRedirectHandler())
    with opener.open(url, timeout=timeout) as r:  # noqa: S310 — validated above
        raw = r.read(_MAX_FETCH_BYTES + 1)
    if len(raw) > _MAX_FETCH_BYTES:
        raise ValueError(
            f"fetched document exceeds the {_MAX_FETCH_BYTES}-byte limit"
        )
    return raw


def _converter():
    """Lazily build the markitdown converter (avoid import cost at
    tools/list time).
    """
    global _md
    if _md is None:
        from markitdown import MarkItDown

        _md = MarkItDown()
    return _md


def _load_workspace_bytes(agent_id: str | None, path: str, binding: str = "") -> bytes:
    """Read an uploaded workspace file's CONTENT.

    This is a SHARED runner that mounts no agent work volume, so a `path`
    cannot be `open()`-ed locally in production. Try a local mount first
    (dev/test where CERASE_TOOL_WORKSPACE_ROOT IS the agent's workspace), then
    fall back to the control-plane internal API (it owns workspace access via
    docker exec) scoped to (agent_id, path).
    """
    try:
        local = _safe_local_path(path)
        if os.path.isfile(local):
            with open(local, "rb") as f:
                return f.read()
    except ValueError:
        pass  # not a safe local path → let the control-plane re-guard + serve

    cp = os.environ.get("CERASE_CONTROL_PLANE_URL", "").rstrip("/")
    secret = os.environ.get("CERASE_INTERNAL_SECRET", "")
    if not agent_id or not cp or not secret:
        raise ValueError(
            "workspace `path` given but no local file and no control-plane "
            "configured (agent_id / CERASE_CONTROL_PLANE_URL / CERASE_INTERNAL_SECRET)"
        )
    from urllib.parse import urlencode

    qs = urlencode({"path": path})
    # The control-plane broker requires the calling agent's binding
    # (gateway-injected tool arg) besides the shared bearer.
    headers = {"Authorization": f"Bearer {secret}"}
    if binding:
        headers["X-Cerase-Agent-Binding"] = binding
    req = urllib.request.Request(
        f"{cp}/api/internal/workspace-file/{agent_id}?{qs}",
        headers=headers,
    )
    with urllib.request.urlopen(req, timeout=30) as r:  # noqa: S310 — internal API
        return r.read()


# The extensions this tool will hand to the converter, measured rather than
# declared: each one was converted under `--network none` from a fixture the
# test generates, and each produced extracted text.
#
# An ALLOWLIST and not a denylist, because the converter routes by extension and
# grows converters on its own. `read_document` used to pass anything through,
# which is how a connector declared `auth.kind: none` and preinstalled on every
# assistant came to send the caller's bytes to a third party: markitdown's audio
# converter calls Google's speech API, so a customer's recording left the
# appliance the moment somebody asked their assistant to read it. Proven under
# `--network none`: it dies on the DNS lookup.
#
# ODT and RTF are deliberately absent and were advertised for months. Measured:
# ODT raises `UnsupportedFormatException`, and RTF comes back byte-identical to
# the file that went in — the markup, not the text in it. Making them work is an
# engine swap, not this.
SUPPORTED_SUFFIXES: frozenset[str] = frozenset(
    {".pdf", ".docx", ".xlsx", ".pptx", ".html", ".htm", ".md", ".epub", ".csv", ".txt", ".json", ".xml"}
)

# Extensions worth naming their own destination. Anything else outside the
# allowlist gets the generic refusal; these get told where the capability is,
# because the assistant is about to tell a person "I cannot", and "I cannot,
# ask me this way instead" is a different sentence.
_REDIRECTED_SUFFIXES: dict[str, str] = {
    ".mp3": "audio", ".wav": "audio", ".m4a": "audio", ".ogg": "audio",
    ".opus": "audio", ".flac": "audio", ".aac": "audio", ".wma": "audio",
    ".mp4": "audio", ".mov": "audio", ".webm": "audio", ".mkv": "audio",
    ".png": "image", ".jpg": "image", ".jpeg": "image", ".gif": "image",
    ".webp": "image", ".bmp": "image", ".tiff": "image",
}


def _suffix_for(file_url: str | None, filename: str | None) -> str:
    if filename and "." in filename:
        return "." + filename.rsplit(".", 1)[1].lower()
    if file_url:
        path = urlparse(file_url).path
        if "." in path:
            return "." + path.rsplit(".", 1)[1].lower()
    return ""


@mcp.tool()
def read_document(
    agent_id: str | None = None,
    path: str | None = None,
    file_url: str | None = None,
    file_base64: str | None = None,
    filename: str | None = None,
    agent_binding: str = "",
) -> dict[str, Any]:
    """Extract text/markdown from a document.

    Use when the user uploads or links a PDF / Office / web document and
    wants its contents read or summarised. Supported: PDF, DOCX, XLSX,
    PPTX, HTML, MD, EPUB, CSV, TXT, JSON, XML. Audio, video and images
    are refused here and belong to cerase-media.

    Args:
        agent_id: Cerase Agent PK — bound by the gateway; required only for
            the `path` form (used to fetch the workspace file's content).
        path: workspace file path (the form the attachment-receiver
            skill uses — the bridge drops uploads into the agent's
            workspace). Use this OR file_url OR file_base64.
        file_url: http(s) URL of the document — public remote hosts
            only (local files must use `path`, not a file:// URL).
        file_base64: a base64 / data-URL payload of the document.
        filename: original filename — gives the extension hint the
            converter uses (recommended when passing base64).
        agent_binding: injected by the platform (second factor for the
            workspace-file broker) — do not set it.

    Returns:
        dict with `text` (extracted markdown) and `format` (the
        extension that was used).
    """
    sources = [s for s in (path, file_url, file_base64) if s]
    if len(sources) != 1:
        raise ValueError("supply exactly one of path / file_url / file_base64")

    suffix = _suffix_for(file_url, filename or path)

    # Refused BEFORE the bytes are written to disk and handed over, because the
    # converter decides what to do with them by this same extension and one of
    # its decisions is a request to a third party.
    if suffix not in SUPPORTED_SUFFIXES:
        kind = _REDIRECTED_SUFFIXES.get(suffix)
        if kind == "audio":
            raise ValueError(
                f"this tool does not read audio or video ({suffix or 'no extension'}). "
                "Transcription belongs to cerase-media, which owns it and keeps the "
                "recording on this appliance."
            )
        if kind == "image":
            raise ValueError(
                f"this tool does not read images ({suffix}). Ask cerase-media to "
                "describe or transcribe the picture instead."
            )
        raise ValueError(
            f"unsupported document type '{suffix or 'no extension'}'. This tool reads: "
            + ", ".join(sorted(x.lstrip('.') for x in SUPPORTED_SUFFIXES))
            + "."
        )

    if path:
        raw = _load_workspace_bytes(agent_id, path, agent_binding)
    elif file_url:
        # Only public http(s) targets — never file:// (LFI) nor
        # loopback/private/metadata addresses (SSRF). Local files go
        # through the broker-scoped `path` form instead.
        raw = _safe_fetch(file_url)
    else:
        payload = file_base64 or ""
        if "," in payload and payload.strip().startswith("data:"):
            payload = payload.split(",", 1)[1]
        raw = base64.b64decode(payload)

    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(raw)
        tmp_path = tmp.name
    try:
        result = _converter().convert(tmp_path)
        text = getattr(result, "text_content", "") or ""
    finally:
        os.unlink(tmp_path)

    return {"text": text, "format": suffix.lstrip(".") or "unknown"}


if __name__ == "__main__":
    mcp.run()
