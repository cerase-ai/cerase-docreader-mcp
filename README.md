# cerase-docreader-mcp

An MCP server that extracts the text of a document as markdown, using
[markitdown](https://github.com/microsoft/markitdown). It calls no model and
needs no credential of its own.

## Tools

| Tool | What it does | Arguments | Returns |
|---|---|---|---|
| `read_document` | Converts one document to markdown text. | exactly one of `path`, `file_url`, `file_base64`; optional `filename`; `agent_id`, `agent_binding` | `{text, format}` |

The converter is chosen by extension, taken from `filename`, else from `path`,
else from the URL. Accepted: `pdf`, `docx`, `xlsx`, `pptx`, `html`, `htm`,
`md`, `epub`, `csv`, `txt`, `json`, `xml`. Any other extension is refused
before the bytes reach the converter. Audio, video and image extensions are
refused with a pointer to `cerase-media`, because markitdown's audio converter
would send the recording to a third-party speech service. ODT and RTF are not
accepted: markitdown fails on ODT and returns RTF markup unconverted.

The three sources:

- `path` is a file in the calling assistant's workspace. The server reads it
  directly when it exists under `CERASE_TOOL_WORKSPACE_ROOT`; otherwise it
  fetches it from the Cerase control-plane at
  `GET /api/internal/workspace-file/<agent_id>?path=…`, presenting
  `CERASE_INTERNAL_SECRET` as a bearer and `agent_binding` as
  `X-Cerase-Agent-Binding`. Inside Cerase the gateway fills `agent_id` and
  `agent_binding`; the model never sets them.
- `file_url` is fetched only over `http` or `https`, and only from a host that
  resolves to a public address: loopback, private, link-local, reserved and
  cloud-metadata addresses are refused, and every redirect is checked again.
  Downloads stop at `CERASE_FETCH_MAX_BYTES`.
- `file_base64` is a base64 payload or a `data:` URL.

## Settings

| Variable | Default | Purpose |
|---|---|---|
| `CERASE_TOOL_WORKSPACE_ROOT` | `/workspace` | Directory a `path` is read from locally; a path resolving outside it is never opened. |
| `CERASE_CONTROL_PLANE_URL` | none | Control-plane base URL for the `path` form when the file is not local. |
| `CERASE_INTERNAL_SECRET` | none | Bearer token for that control-plane request. |
| `CERASE_FETCH_MAX_BYTES` | `67108864` (64 MiB) | Largest document `file_url` may download. |
| `CERASE_FETCH_ALLOWED_HOSTS` | empty | Comma-separated hostnames; when set, `file_url` may reach only these. |

## Installation

The connector is published in the Cerase Marketplace as
`studio.guidance/cerase-docreader`
([marketplace page](https://marketplace.cerase.ai/en/p/studio.guidance/cerase-docreader)).
Every Cerase appliance installs it at boot, so its assistants have it without
an install step.

The image `ghcr.io/cerase-ai/cerase-docreader-mcp` is built and published from
the copy of these files kept in the Cerase appliance repository, which is
private. This repository carries the same files byte for byte, so a change
made only here does not reach the image.

## Build and run locally

```sh
docker build -t cerase-docreader-mcp .
docker run --rm -p 3000:3000 cerase-docreader-mcp
```

The build needs BuildKit (the default builder since Docker 23): it bind-mounts
`test_drive_download.py` and `image_check_drive_download.py` to convert a real
PPTX, DOCX and PDF with the installed markitdown, and a failed conversion
stops the build.

`server.py` speaks MCP over stdio; the image runs it behind `mcp-proxy`, which
serves Streamable HTTP at `http://localhost:3000/mcp` and SSE at
`http://localhost:3000/sse`. The image's `HEALTHCHECK` runs
`scripts/healthcheck.py`, an MCP client that completes the handshake and lists
the tools over `/mcp`; its `CERASE_HEALTHCHECK_*` variables exist to point it
at a stub in tests.

The unit tests stub the MCP SDK and need only the standard library:

```sh
python3 -m unittest discover -p 'test_*.py'
```

## License

MIT. See [LICENSE](LICENSE).
