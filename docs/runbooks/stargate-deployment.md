# Stargate UAT deployment profile

Stargate is NautGate's UAT environment. It is not the production release
channel. Production release artifacts are published through GitHub only after
the exact package has passed UAT and received explicit approval.

The UAT traffic switch completed on 2026-09-05. Clients retain
`http://localhost:8090` while an SSH/Tailscale tunnel reaches Stargate's
loopback-only Core endpoint.

## Current UAT topology

- Compose project: `nautgate-stargate`
- Core: container port 8090, host loopback port 18090
- NautRouter: container port 8404, host loopback port 18404
- PostgreSQL: internal only; no host port
- Database volume: `nautgate-staged-db-data`
- Client rehearsal tunnel: local 18091 to Stargate 18090
- Live client tunnel: local 8090 to Stargate 18090

`deploy/compose.production.yml`, `deploy/profiles/stargate.env`, and the
`nautgate-stargate` project name are legacy identifiers from the migration.
They currently run UAT and must not be interpreted as the GitHub production
release channel. Their controlled rename is planned separately.

Stargate host port 8090 remains assigned to Beszel. NautGate stays on loopback
port 18090 and the Mac Studio provides the stable local endpoint on port 8090.

## Validate without starting anything

```bash
cp deploy/profiles/stargate.env.example deploy/profiles/stargate.env
# Replace REQUIRED values without committing the file.
scripts/validate-deployment-env.sh deploy/profiles/stargate.env

docker compose \
  --env-file deploy/profiles/stargate.env \
  -f deploy/compose.production.yml \
  config --quiet

scripts/validate-image-lock.sh deploy/profiles/stargate.images.lock
```

The image-lock example records the already prepared snapshot. Rename it to
`stargate.images.lock` after the final 0.5.1 images are tagged and transferred.

## Rehearsal tunnel

```bash
NAUTGATE_LOCAL_PORT=18091 scripts/nautgate-remote.sh tunnel-start
curl -fsS http://127.0.0.1:18091/ready
scripts/nautgate-remote.sh tunnel-stop
```

The LaunchAgent example remains disabled and uses port 18091. It is a rehearsal
artifact, not the active UAT tunnel.

## UAT versus production release boundary

Deploying or updating Stargate changes UAT only. It must never create a Git tag,
GitHub release, or published package. GitHub publication is a separate promotion
step using the exact artifacts and digests already accepted in UAT.
