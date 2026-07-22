# Contributing to NautGate

Thanks for considering it. NautGate is small and opinionated — the fastest way
to get a PR merged is to open an issue first and agree the shape.

## Ground rules

- **Discuss before building.** For anything beyond a bug fix, open an issue.
  A rejected 500-line PR wastes your evening, not ours.
- **Small, focused PRs.** One concern per PR. If the description needs the word
  "also", it's probably two PRs.
- **Match the surrounding code.** Don't reformat files you're not changing.
- **Tests for behaviour.** Non-trivial logic needs a test that fails without
  your change.

## Setup

Requires Docker, Python 3.12+ and [uv](https://github.com/astral-sh/uv).

```bash
git clone https://github.com/48Nauts-Operator/NautGate.git
cd NautGate
cp .env.example deploy/.env
docker compose -f deploy/docker-compose.yml up -d nautgate-db
cd core && uv sync
```

Before pushing:

```bash
just test     # pytest — all green
just lint     # ruff check — clean
```

Changes to `vendor/NautRouter` need a container rebuild to take effect:

```bash
cd deploy && docker compose --env-file .env up -d --build nautrouter
```

## Things worth knowing

- **`route_decisions` writes are synchronous** (it's an audit log). Outcomes and
  extension calls are fire-and-forget. Don't "optimise" the first into the second.
- **`actual_model` must come from the upstream response**, never echoed from the
  request. The entire value of the audit log rests on this.
- **Never commit a real API key**, including in tests. Fixtures must be
  shape-only (`ng_0000…`). There is a classifier test that will happily match a
  real token — that is not a reason to paste one.
- **Unpriced ≠ free.** A model missing from `config/pricing.yaml` records `NULL`
  cost and renders as "unpriced". Never substitute `0`.
- Endpoints that don't exist yet return `501` with `X-Nautgate-Coming-In`, not 404.

## Commit messages

Conventional commits, imperative mood:

```
fix(router): pass gpt-* through to OpenAI instead of auto-routing
feat(bench): head-to-head comparison of real calls
docs: clarify the --bare requirement for Claude Code
```

Explain *why* in the body when the change isn't self-evident. The diff already
says what.

## Reporting bugs

Use the issue templates. For routing or model problems, the single most useful
thing you can include is the **decision id** from the Audit Log plus what the
flow view shows — that pins down which hop failed.

## Security

Don't open a public issue for vulnerabilities — see [SECURITY.md](SECURITY.md).

## Contributor License Agreement

By submitting a contribution (a PR, patch, or any code, docs or other material)
you agree to the following:

1. **You own it.** The contribution is your original work, or you have the right
   to submit it. If your employer has rights to work you produce, you have their
   permission to contribute it.

2. **You license it to the project under AGPL-3.0**, the same license as the
   rest of NautGate.

3. **You additionally grant 48Nauts a perpetual, worldwide, irrevocable,
   royalty-free license** to use, reproduce, modify, sublicense and distribute
   your contribution **under any license terms, including commercial and
   proprietary terms.**

Clause 3 is what lets NautGate be offered both as AGPL open source and under a
separate commercial license. Without it, every contributor would hold a veto
over commercial licensing and dual-licensing becomes impossible.

**You keep the copyright to your contribution.** This is a license grant, not an
assignment — you can still use your own code anywhere else, for anything.

If you're not comfortable with clause 3, that's a legitimate position: open an
issue describing the bug or design instead, and someone else can implement it.

Questions: <hello@48nauts.com>.
