# Cerase Docreader MCP — document→text extraction via markitdown.
# TOOLS-2: first-party rebuild (was external kiso-docreader), owned for
# standardization + version-pinning. Makes NO LLM call → unbilled.
#
# Exposes 1 tool: read_document. FastMCP stdio bridged by mcp-proxy —
# same shape as the other cerase-* MCP images.
FROM python:3.13.9-slim@sha256:326df678c20c78d465db501563f3492d17c42a4afe33a1f2bf5406a1d56b0e86

# Some markitdown converters shell out to system tooling for legacy
# formats; ffmpeg/ca-certs keep the common paths working.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt requirements.lock /tmp/
RUN pip install --no-cache-dir -r /tmp/requirements.lock \
    && rm /tmp/requirements.txt /tmp/requirements.lock

COPY server.py /app/server.py

# The liveness probe below is a real MCP client, not a socket connect: with the
# stdio child gone mcp-proxy keeps the listener open and answers the handshake
# from what it recorded at its own startup, so the connect went on passing for a
# container with no server left in it. The script's header carries the detail.
COPY scripts/healthcheck.py /app/healthcheck.py

RUN groupadd -r appuser \
 && useradd -r -g appuser -u 1000 -m -d /home/appuser -s /usr/sbin/nologin appuser \
 && chown -R appuser:appuser /app
USER appuser
WORKDIR /home/appuser

EXPOSE 3000

# M-CI-3: image-level liveness — runtime-spawned MCP containers have no
# compose healthcheck, this is the only signal `docker ps`/doctor sees.
#
# It completes the handshake and lists this server's tools, which is the first
# request that has to reach the stdio child. The script keeps itself inside the
# timeout by budget, so the timeout below is its ceiling and not its schedule.
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD python3 /app/healthcheck.py || exit 1

ENTRYPOINT ["sh", "-c", "exec mcp-proxy --port 3000 --host 0.0.0.0 --pass-environment -- python /app/server.py"]
