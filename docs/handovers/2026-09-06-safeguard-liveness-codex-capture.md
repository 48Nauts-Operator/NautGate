# NautGate UAT handover — 2026-09-06

This handover is the reset-safe continuation point for Safeguard Intelligence,
agent/model liveness, and Codex capture on Stargate UAT.

## Guardrails

- Stargate is UAT.
- GitHub is production. Do not push GitHub, merge `main`, tag, release, or deploy
  production without explicit owner approval.
- Historical safeguard backfill `--apply` is not approved and has not run.
- Preserve the unrelated untracked `research/` and `visualizations/` directories.

## Repository state

- Working branch: `feat/safeguard-intelligence`
- Last deployed feature commit before this handover: `93afbff`
- UAT Core image: `nautgate-uat-core:93afbffe0e5d`
- UAT image digest:
  `sha256:61c0a8b3cc24be3ea5352f3fe736d019690369ce325ee276bcd7d288c2bbfb70`
- Database migration 033 is applied on Stargate.
- Dashboard cache key: `0.5.1-safeguard-liveness-2`

The deployed candidate includes the Safeguard Monitor and APIs, explain/review
actions, anomaly leads, model activity/liveness chart (NAUTGATE-56), and
clickable safeguard observation rows.

## Verification and evidence

- Final verification after the proxy repair: lint and formatting passed, shell
  syntax and diff checks passed, and the full Core suite passed all 608 tests.
- The clickable-row follow-up passed its 10 focused tests.
- Historical dry run: 183,572 eligible responses; 180,979 decoded; 2,593
  incomplete or undecodable; 7 provider-confirmed safeguard events; zero writes.
- UAT remained healthy after deployment and migration.
- Pre-change database archive on NAS:
  `/Volumes/Workspace/docker-archives/mac-studio/2026-09-06-nautgate-safeguard-uat/pre-c4fcb243b303.dump`
- Archive SHA-256:
  `7fe8bf43ae9a1a601d50e4e3d397b04c459beb65943de3502728d0d835a370fa`
- `pg_restore --list` validation passed.

## Codex capture diagnosis and repair

`just -g codex` correctly configured the local TLS proxy, but the already-running
proxy had an empty `NAUTGATE_INGEST_TOKEN`. Completed Codex turns therefore
reached `/v1/ingest` and were rejected with HTTP 401.

The repair makes `scripts/codex-proxy.sh` load the token from
`~/.nautgate/ingest-token` (or `NAUTGATE_INGEST_TOKEN_FILE`), refuse to start
without a token, warn on unsafe file permissions, and report only configured or
missing status. The token value must never be logged or committed. The local
file should be mode 600 and match the token configured on Stargate UAT.

After restarting the shared proxy, start or resume Codex with `just -g codex`,
complete one turn, then verify that a recent `agent_id = codex` decision appears
in NautGate. TLS warnings for the ChatGPT analytics host are separate from the
ingest-token failure.

## Remaining decisions

- Review real safeguard observations and anomaly leads on UAT.
- Decide separately whether to approve historical backfill writes.
- Obtain explicit approval before any GitHub/production push or release.
