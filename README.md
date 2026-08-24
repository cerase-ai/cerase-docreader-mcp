# cerase-docreader MCP

First-party document→text extraction for Cerase agents (TOOLS-2).
Replaced the external `kiso-docreader`. Owned for standardization +
version-pinning even though it makes **no LLM call** — it is therefore
**unbilled** (no token charge, no flat fee).

## Tool

`read_document(file_url?, file_base64?, filename?) → {text, format}`

Exactly one of `file_url` / `file_base64`. Pass `filename` (or use a
URL with an extension) so the converter picks the right reader.

Supported: PDF, DOCX, XLSX, PPTX, HTML, MD, EPUB, CSV, TXT, JSON, XML
(via markitdown). Anything else is refused on its extension, before the
bytes are written to disk.

RTF and ODT were listed here and never worked: ODT raises
`UnsupportedFormatException` and RTF comes back byte-identical to the
file that went in — the markup, not the text in it.

**Audio, video and images are refused, and that is a boundary rather
than a gap.** markitdown routes audio to a converter that calls Google's
speech API, so a file handed here would have left the machine. Ask
`cerase-media` instead; it owns transcription and keeps the recording
where it is.

## Billing

None — docreader makes no upstream LLM call. The tokens that later
*read* the extracted text bill normally through the agent's tier model.

GHCR image `ghcr.io/cerase-ai/cerase-docreader-mcp:<tag>`.
