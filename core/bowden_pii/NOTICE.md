# Bowden PII deterministic runtime

`types.py`, `validators.py`, and `rules.py` are vendored from `bowden-pii`
commit `ebeed97` (2026-08-21), an MIT-licensed 48Nauts project. NautGate ships
only the dependency-free deterministic detector so container builds and offline
installs do not depend on a private Forgejo server or the neural training stack.

Upstream development repository:
`48Nauts/bowden-pii` on the 48Nauts Forgejo instance.

The full MIT license text is included in `LICENSE` in this directory.
