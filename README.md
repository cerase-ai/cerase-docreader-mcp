# cerase-docreader MCP

First-party document→text extraction for Cerase agents (TOOLS-2).
Replaced the external `kiso-docreader`. Owned for standardization +
version-pinning even though it makes **no LLM call** — it is therefore
**unbilled** (no token charge, no flat fee).

## Tool

`read_document(file_url?, file_base64?, filename?) → {text, format}`

Exactly one of `file_url` / `file_base64`. Pass `filename` (or use a
URL with an extension) so the converter picks the right reader.

Supported: PDF, DOCX, XLSX, PPTX, HTML, MD, RTF, ODT, EPUB, CSV (via
markitdown).

## Billing

None — docreader makes no upstream LLM call. The tokens that later
*read* the extracted text bill normally through the agent's tier model.

GHCR image `ghcr.io/cerase-ai/cerase-docreader-mcp:<tag>`.
