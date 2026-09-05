# Stargate deployment profile

This profile is preparation-only until the operator approves cutover. It does
not replace the current local aliases, start the staged stack, or bind a
production-facing port.

## Prepared topology

- Compose project: `nautgate-stargate`
- Core: container port 8090, host loopback port 18090
- NautRouter: container port 8404, host loopback port 18404
- PostgreSQL: internal only; no host port
- Database volume: `nautgate-staged-db-data`
- Client rehearsal tunnel: local 18091 to Stargate 18090

Stargate host port 8090 remains assigned to Beszel. The stable client endpoint
decision is deferred to cutover: either move Beszel and bind NautGate there, or
leave NautGate on 18090 and use the local tunnel on 8090.

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

## Rehearsal-only tunnel

```bash
NAUTGATE_LOCAL_PORT=18091 scripts/nautgate-remote.sh tunnel-start
curl -fsS http://127.0.0.1:18091/ready
scripts/nautgate-remote.sh tunnel-stop
```

The LaunchAgent example has `RunAtLoad` disabled and uses port 18091. Merely
copying it cannot redirect current clients.

## Cutover boundary

Do not run `docker compose up`, bind local port 8090, load the LaunchAgent, or
change aliases as preparation work. Those actions belong to the explicit
cutover and rollback procedure in `docs/runbooks/portable-migrations.md`.
