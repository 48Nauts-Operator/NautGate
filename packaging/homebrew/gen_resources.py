#!/usr/bin/env python3
"""Generate Homebrew `resource` stanzas for the nautgate formula from uv.lock.

Homebrew wants every transitive Python dependency pinned as a `resource` block
with a URL and a sha256. `brew update-python-resources` can normally do this,
but it resolves the formula's own package from PyPI and NautGate is not
published there — so the lockfile is the source of truth instead.

Running this at release time is not optional: a stale resource list installs old
dependencies silently, which is exactly the class of bug that is invisible until
someone else's machine behaves differently from yours.

    uv run --project core python packaging/homebrew/gen_resources.py --write
    uv run --project core python packaging/homebrew/gen_resources.py --check

Dev dependencies and the `proxy` extra are excluded deliberately: mitmproxy is
optional and was a third of the install.
"""

from __future__ import annotations

import argparse
import pathlib
import subprocess
import sys
import tomllib

from packaging.markers import default_environment
from packaging.requirements import Requirement

CORE = pathlib.Path(__file__).resolve().parents[2] / "core"
FORMULA = pathlib.Path(__file__).resolve().parent / "nautgate.rb"
START = "  # BEGIN GENERATED PYTHON RESOURCES\n"
END = "  # END GENERATED PYTHON RESOURCES\n"
TARGET_PYTHON = "3.12"


def target_environments() -> list[dict[str, str]]:
    """Homebrew targets supported macOS architectures with CPython 3.12."""
    environments = []
    for machine in ("arm64", "x86_64"):
        env = default_environment()
        env.update(
            {
                "implementation_name": "cpython",
                "os_name": "posix",
                "platform_machine": machine,
                "platform_python_implementation": "CPython",
                "platform_system": "Darwin",
                "python_full_version": f"{TARGET_PYTHON}.0",
                "python_version": TARGET_PYTHON,
                "sys_platform": "darwin",
            }
        )
        environments.append(env)
    return environments


def runtime_requirements() -> list[tuple[str, str]]:
    """(name, version) for the runtime closure, from uv itself.

    Asking uv rather than walking the lock by hand means the answer matches what
    an install actually resolves. Markers are then evaluated for both macOS
    architectures and the formula's fixed Python version.
    """
    out = subprocess.run(
        ["uv", "export", "--no-dev", "--no-emit-project", "--no-hashes", "--frozen"],
        cwd=CORE,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    reqs: set[tuple[str, str]] = set()
    environments = target_environments()
    for line in out.splitlines():
        line = line.strip()
        if not line or line.startswith(("#", "-")):
            continue
        requirement = Requirement(line)
        if requirement.marker and not any(
            requirement.marker.evaluate(environment=env) for env in environments
        ):
            continue
        version = next(
            (
                specifier.version
                for specifier in requirement.specifier
                if specifier.operator == "=="
            ),
            None,
        )
        if version is None:
            raise ValueError(f"expected an exact pin from uv export: {line}")
        reqs.add((requirement.name, version))

    versions_by_name: dict[str, set[str]] = {}
    for name, version in reqs:
        versions_by_name.setdefault(name.lower(), set()).add(version)
    ambiguous = {
        name: versions
        for name, versions in versions_by_name.items()
        if len(versions) > 1
    }
    if ambiguous:
        details = ", ".join(
            f"{name}={sorted(versions)}" for name, versions in ambiguous.items()
        )
        raise ValueError(
            f"multiple versions required across Homebrew targets: {details}"
        )

    return sorted(reqs, key=lambda item: item[0].lower())


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


def replace_generated_resources(current: str, generated: str) -> str:
    before, marker, remainder = current.partition(START)
    if not marker:
        raise ValueError(f"formula is missing generator marker: {START.strip()}")
    _, marker, after = remainder.partition(END)
    if not marker:
        raise ValueError(f"formula is missing generator marker: {END.strip()}")
    return before + START + generated + END + after


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="fail if the formula is stale")
    ap.add_argument("--write", action="store_true", help="update the formula in place")
    args = ap.parse_args()

    if args.check and args.write:
        ap.error("--check and --write are mutually exclusive")

    text, missing = stanzas()
    if missing:
        print(f"# no sdist for: {', '.join(missing)}", file=sys.stderr)

    generated = text + ("\n" if text else "")
    current = FORMULA.read_text()
    expected = replace_generated_resources(current, generated)

    if args.check:
        if current != expected:
            print("formula resources are stale; run gen_resources.py --write")
            return 1
        print(f"formula resources match uv.lock for macOS/Python {TARGET_PYTHON}")
        return 0

    if args.write:
        FORMULA.write_text(expected)
        print(f"updated {FORMULA}")
        return 0

    print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
