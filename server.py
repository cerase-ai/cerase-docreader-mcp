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
import os
import tempfile
import urllib.request
from typing import Any
from urllib.parse import urlparse

from mcp.server.fastmcp import FastMCP

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


def _converter():
    """Lazily build the markitdown converter (avoid import cost at
    tools/list time).
    """
    global _md
    if _md is None:
        from markitdown import MarkItDown

        _md = MarkItDown()
    return _md


def _load_workspace_bytes(agent_id: str | None, path: str) -> bytes:
    """M-UPLOAD-2 — read an uploaded workspace file's CONTENT.

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
    req = urllib.request.Request(
        f"{cp}/api/internal/workspace-file/{agent_id}?{qs}",
        headers={"Authorization": f"Bearer {secret}"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:  # noqa: S310 — internal API
        return r.read()


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
) -> dict[str, Any]:
    """Extract text/markdown from a document.

    Use when the user uploads or links a PDF / Office / web document and
    wants its contents read or summarised. Supported: PDF, DOCX, XLSX,
    PPTX, HTML, MD, RTF, ODT, EPUB, CSV.

    Args:
        agent_id: Cerase Agent PK — bound by the gateway; required only for
            the `path` form (used to fetch the workspace file's content).
        path: workspace file path (the form the attachment-receiver
            skill uses — the bridge drops uploads into the agent's
            workspace). Use this OR file_url OR file_base64.
        file_url: http(s) URL of the document.
        file_base64: a base64 / data-URL payload of the document.
        filename: original filename — gives the extension hint the
            converter uses (recommended when passing base64).

    Returns:
        dict with `text` (extracted markdown) and `format` (the
        extension that was used).
    """
    sources = [s for s in (path, file_url, file_base64) if s]
    if len(sources) != 1:
        raise ValueError("supply exactly one of path / file_url / file_base64")

    suffix = _suffix_for(file_url, filename or path)

    if path:
        raw = _load_workspace_bytes(agent_id, path)
    elif file_url:
        with urllib.request.urlopen(file_url) as r:  # noqa: S310 — gateway-supplied
            raw = r.read()
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
