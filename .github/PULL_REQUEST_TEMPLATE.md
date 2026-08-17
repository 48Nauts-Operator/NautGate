> 👋 Thanks for contributing! First time here? Skim
> **[CONTRIBUTING.md](https://github.com/48Nauts-Operator/NautGate/blob/main/CONTRIBUTING.md)**
> — issue/PR routing, the CLA, and what makes a PR land fast. AI-assisted PRs are welcome; just mark them.

## What and why

<!-- What changes, and what problem it solves. The diff says what; explain why. -->

Closes #

## How it was verified

<!-- Not "tests pass" — what did you actually exercise? Which endpoint, which
     client, what did you see? If you couldn't test something, say so. -->

- [ ] `just test` green
- [ ] `ruff check` clean (`cd core && uv run ruff check .`)
- [ ] Exercised the real surface (not only unit tests)

## Checklist

- [ ] I have read and agree to the contributor license terms in `CONTRIBUTING.md`
- [ ] User-visible change? `CHANGELOG.md` updated under `## [Unreleased]`
- [ ] One concern — this PR doesn't also do something else
- [ ] No real API keys or captured bodies in code, tests or fixtures
- [ ] Existing behaviour unchanged, or the change is called out below
- [ ] Touched `vendor/NautRouter`? Container rebuilt and verified
- [ ] Touched pricing? Missing entries still record NULL, never `0`

## Breaking changes

<!-- Schema, endpoint shape, config keys. "None" is a fine answer. -->

None.
