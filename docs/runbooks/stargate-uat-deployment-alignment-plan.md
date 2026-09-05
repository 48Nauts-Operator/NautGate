# Stargate UAT deployment alignment plan

## Decision

Stargate is the NautGate UAT runtime. GitHub is the production release and
package publication channel. A UAT deployment must never implicitly publish a
release, and a release must promote the exact artifacts accepted in UAT rather
than rebuilding them.

This plan changes tooling and names only after compatibility checks. It does
not authorize changes to the currently running UAT stack.

## Current state to preserve

- Live client endpoint: `http://localhost:8090`
- Tunnel destination: Stargate `127.0.0.1:18090`
- Router endpoint: Stargate `127.0.0.1:18404`
- Current Compose project: `nautgate-stargate`
- Current final database volume: `nautgate-final-20260905t1652`
- Current environment file on Stargate: `deploy/profiles/stargate.env`
- Current Compose file: `deploy/compose.production.yml`
- Source database retained for the rollback period

The project, file, and profile names above are legacy identifiers. Renaming a
Compose project creates different container/network identities, so it must be
treated as a controlled UAT maintenance change. The external database volume
must never be renamed, copied, deleted, or implicitly recreated as part of the
terminology cleanup.

## Desired operator interface

```text
just deploy-uat       # deploy a candidate package to Stargate only
just verify-uat       # health, routing, database and resource gates
just rollback-uat     # restore the previously accepted UAT image set
just package-release  # create release assets from accepted UAT artifacts
just publish-release  # explicit GitHub tag/release action after approval
```

Every command must print its target and require the expected environment. UAT
commands must fail if the remote host is not Stargate. Release commands must
not connect to Stargate or mutate its database.

## Package contract

One candidate package should contain:

```text
nautgate-VERSION/
├── images/
│   ├── core.tar
│   └── nautrouter.tar
├── deploy/
│   └── compose.uat.yml
├── migrations/
├── manifest.json
└── checksums.sha256
```

`manifest.json` records the version, source commit, build timestamp, required
schema version, image names and digests, and checksums. GitHub publication uses
this exact accepted package and its checksums.

## Implementation phases

### 1. Add UAT names without removing legacy names

- Add `deploy/compose.uat.yml` with the same validated service topology.
- Add `deploy/profiles/uat-stargate.env.example`.
- Add an ignored live `uat-stargate.env` on Stargate with mode `0600`.
- Keep `compose.production.yml` and `stargate.env` temporarily as compatibility
  inputs; mark them deprecated in comments and documentation.
- Default new scripts to `compose.uat.yml`, profile `uat-stargate.env`, and
  project `nautgate-uat` only after the controlled project transition.

Acceptance: both old and new Compose configurations render to equivalent
service images, ports, limits, environment names, external volumes, and health
checks. Secret values are never printed.

### 2. Separate UAT deployment from release publication

- Add a UAT deploy script that accepts a versioned package and remote host.
- Add a release packaging script that performs no deployment.
- Add a GitHub publishing script/workflow requiring an explicit version and
  accepted manifest checksum.
- Remove ambiguous `production` wording from UAT command output.

Acceptance: dry runs prove that `deploy-uat` cannot tag or publish and that
`publish-release` cannot start, stop, or recreate Stargate services.

### 3. Add deployment gates

Before UAT deployment:

- Verify Git status and source commit.
- Verify package and image checksums.
- Verify Stargate identity, Colima context, disk, memory, and existing services.
- Back up PostgreSQL when a package contains schema migrations.
- Record current image digests and database schema version for rollback.

After UAT deployment:

- Require Core `/health` and `/ready`, Router `/health`, and dashboard HTTP 200.
- Require healthy containers and zero unexpected restarts/OOM events.
- Run an `ng_`-authenticated provider request.
- Run Claude Max/OAuth verification and require `anthropic-oauth`, HTTP 200,
  and subscription cost `$0`.
- Run a synthetic confidential-routing request through LM Studio.
- Confirm new audit rows exist only in UAT PostgreSQL.

### 4. Make service connections runtime-configurable

- Add a `Services & Connections` settings model and UI.
- Store a stable LM Studio endpoint such as
  `http://cand0rians-mac-studio.tail138398.ts.net:1238`.
- Make Core discovery, confidential routing, health checks, and NautRouter use
  the same connection record.
- Store credential references, not plaintext credentials, in UI responses.
- Support test connection, last success, current error, discovered models, and
  dependent routing policies.
- Preserve fail-closed confidential routing when LM Studio is unavailable.

Until dynamic configuration exists, both the Core discovery endpoint and
NautRouter inference endpoint must be supplied through the UAT profile and a
service recreation is required to change them.

### 5. Controlled Compose project transition

Schedule a short UAT maintenance window:

1. Back up UAT PostgreSQL and verify the backup checksum.
2. Confirm `nautgate-final-20260905t1652` is declared as the same external
   volume in the new configuration.
3. Stop the legacy `nautgate-stargate` application containers without removing
   volumes.
4. Start project `nautgate-uat` from the accepted images and new UAT profile.
5. Run all deployment gates before reopening the tunnel.
6. Retain the legacy configuration and image digests for rollback.

Never use `docker compose down -v` during this transition.

### 6. Promotion to GitHub production release

After UAT acceptance:

1. Freeze the accepted manifest and checksums.
2. Confirm GitHub tag/version does not already exist.
3. Create the signed tag from the tested commit.
4. Publish the exact UAT-tested package and checksums as GitHub release assets.
5. Record the UAT decision IDs, image digests, package checksum, tag, and GitHub
   release URL in the release evidence.

## Rollback requirements

- Keep the prior image digests and UAT profile.
- Never delete the database volume during application rollback.
- If no migration ran, recreate Core and Router using prior image digests.
- If a reversible migration ran, execute its tested downgrade before restoring
  prior images.
- If a migration is not reversible, restore the verified pre-deployment backup
  into a new volume and validate before switching the UAT stack.
- GitHub release rollback is separate: deprecate or replace a release; it must
  not mutate the UAT runtime automatically.

## Documentation completion criteria

- All docs describe Stargate as UAT.
- All docs describe GitHub as the production release channel.
- Historical migration evidence retains the actual legacy filenames and project
  names, clearly labelled as legacy.
- Operator commands distinguish deploy, verify, rollback, package, and publish.

