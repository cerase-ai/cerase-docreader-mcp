"""A file saved from Drive into the workspace reads through its path.

The gateway saves a Drive download at `downloads/<name>` in the calling
assistant's workspace and answers with that path and this tool. This runner
mounts no workspace, so `read_document(path=...)` fetches the bytes from the
control-plane's workspace-file broker for the calling agent, and the converter
is chosen by the path's extension.

Two layers. The first runs everywhere and needs no converter: the relative
path reaches the broker as the gateway wrote it, with the agent and its
binding, and the converter is handed the exact bytes under the right extension.
The second converts a real PowerPoint, Word and PDF file built here, and needs
markitdown, which the runner's own environment has and the host's may not.
"""
from __future__ import annotations

import io
import os
import sys
import tempfile
import types
import unittest
import zipfile
from types import SimpleNamespace
from unittest import mock
from urllib.parse import parse_qs, urlparse

# The same stub test_server.py installs, so `import server` needs no MCP deps.
if "mcp.server.fastmcp" not in sys.modules:
    class _FastMCP:
        def __init__(self, *args, **kwargs):
            pass

        def tool(self, *args, **kwargs):
            def deco(fn):
                return fn

            return deco

        def run(self, *args, **kwargs):
            pass

    _fastmcp = types.ModuleType("mcp.server.fastmcp")
    _fastmcp.FastMCP = _FastMCP
    _mcp_server = types.ModuleType("mcp.server")
    _mcp_server.__path__ = []
    _mcp = types.ModuleType("mcp")
    _mcp.__path__ = []
    sys.modules.setdefault("mcp", _mcp)
    sys.modules.setdefault("mcp.server", _mcp_server)
    sys.modules["mcp.server.fastmcp"] = _fastmcp

import server  # noqa: E402

CANARY = "CANARY-DRIVE"

_ENV = {
    "CERASE_CONTROL_PLANE_URL": "http://cerase-control-plane:8000",
    "CERASE_INTERNAL_SECRET": "the-bearer",
}


def _pdf() -> bytes:
    return (
        b"%PDF-1.4\n"
        b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
        b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
        b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]/Contents 4 0 R"
        b"/Resources<</Font<</F1 5 0 R>>>>>>endobj\n"
        b"4 0 obj<</Length 54>>stream\n"
        b"BT /F1 12 Tf 20 100 Td (" + CANARY.encode() + b") Tj ET\n"
        b"endstream\nendobj\n"
        b"5 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj\n"
        b"trailer<</Root 1 0 R/Size 6>>\n"
    )


def _docx() -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/'
            '2006/content-types"><Default Extension="rels" ContentType="application/vnd.'
            'openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType='
            '"application/xml"/><Override PartName="/word/document.xml" ContentType="application/'
            'vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>',
        )
        z.writestr(
            "_rels/.rels",
            '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/'
            'package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.'
            'openxmlformats.org/officeDocument/2006/relationships/officeDocument"'
            ' Target="word/document.xml"/></Relationships>',
        )
        z.writestr(
            "word/document.xml",
            '<?xml version="1.0"?><w:document xmlns:w="http://schemas.openxmlformats.org/'
            'wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>'
            + CANARY
            + "</w:t></w:r></w:p></w:body></w:document>",
        )
    return out.getvalue()


def _pptx() -> bytes:
    # Built with the library the converter reads it with: a hand-written
    # minimal pptx does not parse.
    from pptx import Presentation
    from pptx.util import Inches

    pres = Presentation()
    slide = pres.slides.add_slide(pres.slide_layouts[5])
    slide.shapes.title.text = "Metodologia"
    box = slide.shapes.add_textbox(Inches(1), Inches(2), Inches(6), Inches(1))
    box.text_frame.text = CANARY
    out = io.BytesIO()
    pres.save(out)
    return out.getvalue()


class _Broker:
    """Stands in for the control-plane's workspace-file GET."""

    def __init__(self, files: dict[str, bytes]) -> None:
        self.files = files
        self.requests: list[object] = []

    def urlopen(self, req, timeout=None):
        self.requests.append(req)
        path = parse_qs(urlparse(req.full_url).query)["path"][0]
        resp = mock.MagicMock()
        resp.read.return_value = self.files[path]
        resp.__enter__ = mock.Mock(return_value=resp)
        resp.__exit__ = mock.Mock(return_value=False)
        return resp


class _Elsewhere(unittest.TestCase):
    """Runs each case from a folder holding no workspace file, so the path
    can only be read through the broker, as in the runner."""

    def setUp(self) -> None:
        self._cwd = os.getcwd()
        self._dir = tempfile.TemporaryDirectory()
        os.chdir(self._dir.name)

    def tearDown(self) -> None:
        os.chdir(self._cwd)
        self._dir.cleanup()


class TheDownloadPathReachesTheBroker(_Elsewhere):
    def test_each_format_is_fetched_for_the_agent_and_converted_by_its_extension(self):
        for path, raw in (
            ("downloads/abc_metodologia.pptx", b"PK\x03\x04pptx-bytes\x00\xff"),
            ("downloads/contratto.docx", b"PK\x03\x04docx-bytes\x00\xff"),
            ("downloads/offerta.pdf", b"%PDF-1.4\x00\xff"),
        ):
            with self.subTest(path=path):
                broker = _Broker({path: raw})
                handed: list[tuple[str, bytes]] = []

                def convert(tmp_path: str):
                    with open(tmp_path, "rb") as f:
                        handed.append((os.path.splitext(tmp_path)[1], f.read()))
                    return SimpleNamespace(text_content="extracted")

                converter = mock.MagicMock()
                converter.convert.side_effect = convert
                with mock.patch.dict(os.environ, _ENV), mock.patch(
                    "urllib.request.urlopen", side_effect=broker.urlopen
                ), mock.patch.object(server, "_converter", return_value=converter):
                    out = server.read_document(agent_id="7", path=path, agent_binding="b7")

                (req,) = broker.requests
                url = urlparse(req.full_url)
                self.assertEqual(url.path, "/api/internal/workspace-file/7")
                self.assertEqual(parse_qs(url.query)["path"], [path])
                self.assertEqual(req.get_header("Authorization"), "Bearer the-bearer")
                self.assertEqual(req.get_header("X-cerase-agent-binding"), "b7")
                extension = os.path.splitext(path)[1]
                self.assertEqual(handed, [(extension, raw)])
                self.assertEqual(out, {"text": "extracted", "format": extension[1:]})
