# Mac Studio Docker pre-prune archive — 2026-09-04

This archive was created before any Docker image or volume pruning associated
with the NautGate migration.

## Location

`/Volumes/Workspace/docker-archives/mac-studio/2026-09-04-nautgate-pre-prune`

The location is on the COSMOS `Workspace` SMB share, not inside Colima.

## Contents

- `images/unreferenced-images.tar.gz`: one Docker save archive containing 24
  image IDs that had no container references at selection time.
- `volumes/*.tar.gz`: 46 individual archives for unreferenced volumes
  containing backups, PostgreSQL, Redis, or other data.
- `manifests/images.tsv`: image IDs, timestamps, sizes, tags, and digests.
- `manifests/volumes-to-archive.tsv`: archived volume names, source sizes, and
  content classifications.
- `manifests/volumes-inspect.json`: Docker metadata for archived volumes.
- `manifests/rebuildable-node-modules.tsv`: 32 unreferenced dependency-cache
  volumes intentionally not archived.
- `manifests/referenced-volumes-excluded.txt`: volumes excluded because at
  least one existing container referenced them.
- `manifests/SHA256SUMS`: checksums for all 47 archives.

The dependency-cache volumes contained `node_modules`, not application data.
They can be recreated from their projects' package manifests and lock files.

## Verify the NAS archive

```bash
cd /Volumes/Workspace/docker-archives/mac-studio/2026-09-04-nautgate-pre-prune
shasum -a 256 -c manifests/SHA256SUMS
```

All archives passed both `gzip -t` and complete `tar -tzf` traversal when the
archive was created.

## Recover images

Loading the image archive restores every saved image object and its saved tags:

```bash
gzip -dc \
  /Volumes/Workspace/docker-archives/mac-studio/2026-09-04-nautgate-pre-prune/images/unreferenced-images.tar.gz \
  | docker load
```

Compare the restored IDs and tags with `manifests/images.tsv`.

## Recover a volume

Replace `VOLUME_NAME` with a name from
`manifests/volumes-to-archive.tsv`. Refuse an existing target volume so recovery
cannot overwrite current data.

```bash
archive_root=/Volumes/Workspace/docker-archives/mac-studio/2026-09-04-nautgate-pre-prune
volume_name=VOLUME_NAME

! docker volume inspect "$volume_name" >/dev/null 2>&1
docker volume create "$volume_name"
mountpoint=$(docker volume inspect "$volume_name" --format '{{.Mountpoint}}')
gzip -dc "$archive_root/volumes/$volume_name.tar.gz" \
  | colima ssh -- sudo tar -C "$mountpoint" -xf -
```

After restoration, recreate the owning service from its Compose project. A raw
PostgreSQL volume must be opened with a compatible PostgreSQL major version;
inspect `PG_VERSION` before starting it. Do not attach a restored volume to a
running service.

## Safety boundary

Archive presence does not authorize pruning. Before deletion, recheck that
each selected image or volume remains unreferenced and obtain explicit operator
approval for the exact IDs. Running containers and all referenced volumes are
outside the pruning scope.

## Pruning record

On 2026-09-04, after archive integrity and reference checks passed, the
operator approved pruning of the archived set. Exactly 46 archived volume names
and 24 archived image IDs were removed. The 32 unarchived `node_modules`
volumes, all referenced volumes, all containers, and all other images were
left intact.

Post-prune verification:

- Colima free space increased from 0 to approximately 12 GB.
- None of the 70 exact archived targets remained locally.
- `nautgate-db` recovered and reported healthy.
- NautGate `/health` and `/ready` both returned HTTP 200.
- The COSMOS archive and its 47 checksums remained present.
