#!/usr/bin/env python3
"""Generate Homebrew `resource` stanzas for the nautgate formula from uv.lock.

Homebrew wants every transitive Python dependency pinned as a `resource` block
with a URL and a sha256. `brew update-python-resources` can normally do this,
but it resolves the formula's own package from PyPI and NautGate is not
published there — so the lockfile is the source of truth instead.

Running this at release time is not optional: a stale resource list installs old
dependencies silently, which is exactly the class of bug that is invisible until
someone else's machine behaves differently from yours.

    python packaging/homebrew/gen_resources.py            # print the stanzas
    python packaging/homebrew/gen_resources.py --check     # exit 1 if the
                                                           # formula is stale

Dev dependencies and the `proxy` extra are excluded deliberately: mitmproxy is
optional and was a third of the install.
"""

from __future__ import annotations

import argparse
import pathlib
import re
import subprocess
import sys
import tomllib

CORE = pathlib.Path(__file__).resolve().parents[2] / "core"
FORMULA = pathlib.Path(__file__).resolve().parent / "nautgate.rb"


def runtime_requirements() -> list[tuple[str, str]]:
    """(name, version) for the runtime closure, from uv itself.

    Asking uv rather than walking the lock by hand means the answer matches what
    an install actually resolves, including markers.
    """
    out = subprocess.run(
        ["uv", "export", "--no-dev", "--no-emit-project", "--no-hashes", "--frozen"],
        cwd=CORE,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    reqs = []
    for line in out.splitlines():
        line = line.strip()
        if not line or line.startswith(("#", "-")):
            continue
        m = re.match(r"^([A-Za-z0-9._-]+)==([^\s;]+)", line)
        if m:
            reqs.append((m.group(1), m.group(2)))
    return reqs


def lock_index() -> dict[tuple[str, str], dict]:
    lock = tomllib.loads((CORE / "uv.lock").read_text())
    return {(p["name"].lower(), p["version"]): p for p in lock.get("package", [])}


def stanzas() -> tuple[str, list[str]]:
    index = lock_index()
    blocks, missing = [], []
    for name, version in sorted(runtime_requirements()):
        pkg = index.get((name.lower(), version))
        sdist = (pkg or {}).get("sdist") or {}
        url, digest = sdist.get("url"), sdist.get("hash", "")
        if not url or not digest.startswith("sha256:"):
            # Wheel-only packages cannot be built from source by Homebrew's
            # virtualenv helper. Naming them beats emitting a broken stanza.
            missing.append(f"{name}=={version}")
            continue
        blocks.append(
            f'  resource "{name}" do\n'
            f'    url "{url}"\n'
            f'    sha256 "{digest.split(":", 1)[1]}"\n'
            f"  end\n"
        )
    return "\n".join(blocks), missing


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="fail if the formula is stale")
    args = ap.parse_args()

    text, missing = stanzas()
    if missing:
        print(f"# no sdist for: {', '.join(missing)}", file=sys.stderr)

    if args.check:
        current = FORMULA.read_text()
        have = set(re.findall(r'resource "([^"]+)" do', current))
        want = set(re.findall(r'resource "([^"]+)" do', text))
        if have != want:
            print(f"formula is stale: missing {sorted(want - have)}, extra {sorted(have - want)}")
            return 1
        print("formula resources match uv.lock")
        return 0

    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
