# sb-capture

NautGate reference extension. Receives `on_request`, `on_response`,
`on_outcome`, and `after_route` hooks and writes each payload as one
NDJSON line to `$SB_CAPTURE_OUTPUT_PATH` (default
`/var/lib/sb-capture/events.ndjson`).

## Run standalone

```bash
cd extensions/sb-capture
uv sync
SB_CAPTURE_OUTPUT_PATH=/tmp/sb-capture.ndjson uv run uvicorn main:app --port 8001
```

Then POST any JSON body to `/v1/on_request`, `/v1/on_response`,
`/v1/on_outcome`, or `/v1/after_route`. Every record adds one line to the file.

## Run via docker compose

`deploy/docker-compose.with-extensions.yml` is an opt-in overlay that brings
this up alongside `nautgate-db` and `nautrouter`:

```bash
docker compose \
  -f deploy/docker-compose.yml \
  -f deploy/docker-compose.with-extensions.yml \
  up -d
```

## Wire into NautGate

In `nautgate.yaml`:

```yaml
extensions:
  sb-capture:
    base_url: http://sb-capture:8001
    hooks: [on_request, on_response, on_outcome, after_route]
    timeout_ms: 200
```

Set `NAUTGATE_CONFIG_PATH=/etc/nautgate/nautgate.yaml` (or wherever you
mount the file).

## Output format

```
{"hook": "on_request", "received_at": 1778..., "payload": {...}}
{"hook": "on_response", "received_at": 1778..., "payload": {...}}
{"hook": "on_outcome", "received_at": 1778..., "payload": {...}}
```

One line per hook invocation. Easy to consume with `jq`, DuckDB, or pandas.
