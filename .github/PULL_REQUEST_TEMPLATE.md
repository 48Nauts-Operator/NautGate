## What and why

<!-- What changes, and what problem it solves. The diff says what; explain why. -->

Closes #

## How it was verified

<!-- Not "tests pass" — what did you actually exercise? Which endpoint, which
     client, what did you see? If you couldn't test something, say so. -->

- [ ] `just test` green
- [ ] `just lint` clean
- [ ] Exercised the real surface (not only unit tests)

## Checklist

- [ ] One concern — this PR doesn't also do something else
- [ ] No real API keys or captured bodies in code, tests or fixtures
- [ ] Existing behaviour unchanged, or the change is called out below
- [ ] Touched `vendor/NautRouter`? Container rebuilt and verified
- [ ] Touched pricing? Missing entries still record NULL, never `0`

## Breaking changes

<!-- Schema, endpoint shape, config keys. "None" is a fine answer. -->

None.
