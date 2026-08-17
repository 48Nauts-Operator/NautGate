# Contributing to NautGate

Thanks for considering it. NautGate is small and opinionated — the fastest way
to get a PR merged is to open an issue first and agree the shape.

## Where development happens

Maintainer development is coordinated on a private Forgejo instance. GitHub is
the supported public home for releases, issues, discussions, and outside
contributions. Open pull requests against `main`; accepted changes land on the
public release lineage with their authorship preserved.

## Ground rules

- **Discuss before building.** For anything beyond a bug fix, open an issue.
  A rejected 500-line PR wastes your evening, not ours.
- **Credit borrowed mechanisms.** Name the source project, file or symbol and
  its license in the file header. Code copied without attribution is not merged.
- **Small, focused PRs.** One concern per PR. If the description needs the word
  "also", it's probably two PRs.
- **Match the surrounding code.** Don't reformat files you're not changing.
- **Tests for behaviour.** Non-trivial logic needs a test that fails without
  your change.

## Where things go

- **Bug or regression** → an issue via the templates. For routing or wrong-model
  problems, the **decision id** from the Audit Log plus the flow view is the
  single most useful thing you can attach.
- **Feature or architecture change** → open an issue or a
  [Discussion](https://github.com/48Nauts-Operator/NautGate/discussions) and
  agree the shape *before* writing code.
- **Question, setup help, an idea** →
  [Discussions](https://github.com/48Nauts-Operator/NautGate/discussions), not
  an issue.
- **Security vulnerability** → privately, see [SECURITY.md](SECURITY.md). Never a
  public issue.

### Please don't open a PR for

- **Refactors nobody asked for.** Match the surrounding code; style-only churn
  gets closed.
- **Test or CI tweaks chasing a failure that's already red on `main`.** It's a
  known issue — if you've found a *new* regression, report it as one.
- **Drive-by "security" findings from an AI scanner** with no reproduction and no
  demonstrated impact. [SECURITY.md](SECURITY.md) says what a real report needs.

## AI-assisted PRs are welcome

Built it with Claude, Codex or another agent? Good — just say so. In the PR:

- Mark it as AI-assisted in the title or description.
- Confirm you actually understand what the code does. You're the author; the
  model isn't.
- Include the useful validation — a test that fails without the change, command
  output, a screenshot. Prompts or session logs are a bonus, not required.

The bar is the same as any PR: correct, focused, and something you'll stand
behind in review. Marking it just tells the reviewer what to look for.

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

## Changelog

Anything a user would notice gets an entry in `CHANGELOG.md` under
`## [Unreleased]`, in the same PR as the code. Internal refactors, test-only
changes and dependency bumps don't need one.

NautGate's whole claim is *evidence* — an attested record of what each call did
and cost. So the bar for an entry is higher than "fixed a bug":

- **Symptom first**, in terms a user would recognise — what did they see?
- **The mechanism**, not the file you touched. "The read timeout was hard-coded
  to 120 s" tells someone whether it hit them; "fixed timeout handling" doesn't.
- **Who it affected.** "Fired on most real launches" and "only with a local
  proxy" are very different facts, and the reader needs the difference.
- **If a number the product reports changed, give both values.** Someone who saw
  $38.82 yesterday and $234.64 today must be able to find out why here.
- **If it was our regression, say so** and link the PR that introduced it.

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

2. **You license it to the project under AGPL-3.0-or-later**, the same license
   as the rest of NautGate.

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
