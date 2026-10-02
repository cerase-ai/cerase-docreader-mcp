"""The office files a Drive download brings read, converted for real.

markitdown converts them and it lives only in this connector's image, so the
image build runs this module (see the Dockerfile) and a conversion that breaks
stops the image. It is not named `test_*.py` on purpose: host python3, which
runs the connector's suite in the unit tier, has no markitdown, and a case it
could only skip would report success for having looked at nothing.
"""

from __future__ import annotations

import os
import unittest
from unittest import mock

from test_drive_download import _ENV, CANARY, _Broker, _docx, _Elsewhere, _pdf, _pptx  # noqa: F401

import server


class AnOfficeFileFromDriveReads(_Elsewhere):
    def test_powerpoint_word_and_pdf_read_through_their_workspace_path(self):
        files = {
            "downloads/abc_metodologia.pptx": _pptx(),
            "downloads/contratto.docx": _docx(),
            "downloads/offerta.pdf": _pdf(),
        }
        broker = _Broker(files)
        with mock.patch.dict(os.environ, _ENV), mock.patch(
            "urllib.request.urlopen", side_effect=broker.urlopen
        ):
            for path in files:
                with self.subTest(path=path):
                    out = server.read_document(agent_id="7", path=path, agent_binding="b7")
                    self.assertIn(CANARY, out["text"])
                    self.assertEqual(out["format"], path.rsplit(".", 1)[1])


if __name__ == "__main__":
    unittest.main()
