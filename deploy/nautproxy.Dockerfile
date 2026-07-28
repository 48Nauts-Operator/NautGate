# nautproxy — forward-proxy sidecar that tees uncooperative clients (clients that
# ignore OPENAI_BASE_URL but honour HTTPS_PROXY + a trusted CA, e.g. Codex in
# ChatGPT-OAuth mode) into NautGate's /v1/ingest. Standalone: just mitmproxy +
# the addon, which is pure stdlib — no NautGate/app code, no DB access.
#
# Build context is the repo root (the addon lives under core/proxy/).
FROM mitmproxy/mitmproxy:latest

COPY core/proxy/codex_capture.py /addon/codex_capture.py

# CA persists in /home/mitmproxy/.mitmproxy — mount a volume there so you trust it
# once, and copy the cert out from the same place.
ENTRYPOINT ["mitmdump", "-s", "/addon/codex_capture.py", \
    "--listen-host", "0.0.0.0", "--listen-port", "8092", \
    "--set", "stream_large_bodies=100m", "--flow-detail", "0"]
