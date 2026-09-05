# Portable NautGate migrations

**Purpose:** make host-to-host moves predictable, reversible, and progressively
closer to zero downtime.

**Current baseline (2026-09-05):** Stargate is the active NautGate UAT host.
Core, PostgreSQL, and NautRouter run in Stargate Colima; Mac Studio clients use
the stable `localhost:8090` endpoint through an SSH/Tailscale tunnel. The old
source Core is stopped and the source database is retained temporarily for
rollback. GitHub, not Stargate, is the production release channel.

## What the preparation taught us

- A hybrid native/container deployment adds host-specific start, status, and
  log paths.
- Client aliases are coupled to both `localhost:8090` and a script that starts
  local infrastructure.
- The database is about 18 GB, dominated by retained prompt/tool payloads, so a
  compressed dump and restore determines most of the outage window.
- Mutable image tags and host credential helpers make a target less
  reproducible.
- Port assignments must be planned: Stargate already uses host port 8090.
- A verified backup is not enough by itself; an isolated restore and content
  comparison are required.

## Improvement backlog

| Improvement | Safe during this migration? | Cutover impact |
| --- | --- | --- |
| Maintain one UAT Compose stack for Core, Router, and PostgreSQL | Yes, prepare and validate while stopped | Start it only at cutover |
| Keep clients on one stable local endpoint through a managed SSH tunnel | Yes, install disabled and test on alternate ports | Enable/change destination at cutover |
| Separate client commands from server lifecycle commands | Yes, add commands without changing existing aliases | Point aliases at new commands at cutover |
| Automate backup, transfer, restore, comparison, and rollback preparation | Yes, default to dry-run/preparation mode | Explicit cutover flag only |
| Add payload retention, compression, or object storage | Design and measure only | Apply after migration and backup validation |
| Support temporary PostgreSQL logical/streaming replication | Design and test in an isolated environment | Source configuration and final promotion are operational changes |
| Pin deployment images by release and digest | Yes | None if hashes match the prepared images |
| Define a migration-safe secret bundle and validation workflow | Yes, without rotating live secrets | Activate/rotate only during a controlled window |
| Keep a host-specific deployment manifest with ports and resource limits | Yes | Start at cutover |
| Rehearse restore, cutover, health checks, and rollback periodically | Yes on isolated ports and volumes | None until an actual exercise promotes traffic |

The concrete inactive deployment profile is documented in
[Stargate deployment profile](stargate-deployment.md).

## Safe work before cutover

The following work must not stop, restart, reconfigure, or redirect the current
NautGate instance:

1. Commit the UAT Compose and host override files.
2. Pin and record image digests.
3. Add a migration command whose default behavior is preparation-only.
4. Add an SSH-tunnel LaunchAgent in a disabled state and test it against the
   staged alternate port.
5. Add remote-aware status and logs commands while preserving existing aliases.
6. Document secrets required by name; never commit their values.
7. Run restore drills using new volume names and unpublished or alternate
   ports.
8. Capture capacity, checksum, schema, row-count, health, and rollback evidence.

## Changes reserved for cutover

1. Pause or drain writes on the source.
2. Perform the final database synchronization.
3. Start the prepared stack and validate `/health`, `/ready`, database content,
   provider routing, and dashboard access.
4. Enable the tunnel or redirect the stable endpoint.
5. Change lifecycle aliases so they no longer start local infrastructure.
6. Keep the old stack stopped but intact until the rollback window expires.

## Follow-up changes after stabilization

Retention/deletion, payload relocation, PostgreSQL replication defaults, secret
rotation, and removal of the old database are intentionally excluded from the
migration itself. They change durability or recovery behavior and require their
own tested rollback paths.

## Definition of a migration-ready release

- A fresh target can be prepared without editing global Docker configuration.
- Every image has a recorded immutable digest.
- Backup archives have checksums and pass a full isolated restore.
- Target row counts and schema checks match the backup snapshot.
- Preparation publishes no externally reachable UAT ports and redirects no clients.
- Cutover and rollback are explicit, separate commands.
- Existing clients use a stable endpoint independent of the hosting machine.
- The old source remains recoverable through the agreed rollback window.
