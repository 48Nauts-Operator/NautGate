"""``nautgate`` command line entry point.

Until now the only way to start the gateway was to remember the uvicorn
incantation or run ``scripts/nautgate.sh`` from a checkout. A native install
(Homebrew, or anything that is not Docker) needs a real command: something to
put on PATH and something for ``brew services`` to exec.

Deliberately small. Starting and inspecting the gateway is all that belongs
here; anything richer belongs in the dashboard, which already exists.
"""

from __future__ import annotations

import argparse
import os
import sys

from app.version import get_version

# A native install has no container boundary around it. Binding every interface
# by default is how a dashboard ends up readable from the whole LAN, so the
# default is loopback and exposing it is an explicit choice.
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8090


def _serve(args: argparse.Namespace) -> int:
    try:
        import uvicorn
    except ImportError:  # pragma: no cover - only reachable on a broken install
        print("uvicorn is not installed — reinstall nautgate", file=sys.stderr)
        return 1

    host = args.host or os.environ.get("NAUTGATE_HOST") or DEFAULT_HOST
    port = args.port or int(os.environ.get("NAUTGATE_PORT") or DEFAULT_PORT)
    if host == "0.0.0.0":  # noqa: S104 - the warning is the point
        print(
            "warning: binding 0.0.0.0 exposes the dashboard to your whole network. "
            "Anyone who can reach this port can read the audit log.",
            file=sys.stderr,
        )
    uvicorn.run("app.main:app", host=host, port=port, reload=args.reload)
    return 0


def _status(args: argparse.Namespace) -> int:
    import json
    import urllib.error
    import urllib.request

    host = args.host or DEFAULT_HOST
    port = args.port or DEFAULT_PORT
    url = f"http://{host}:{port}/health"
    try:
        with urllib.request.urlopen(url, timeout=5) as r:  # noqa: S310 - fixed scheme
            body = json.loads(r.read().decode())
        print(f"running — version {body.get('version', '?')} on {host}:{port}")
        return 0
    except urllib.error.URLError as exc:
        # Report the address that was probed: "not running" is unhelpful when
        # the gateway is up on a port the caller did not ask about.
        print(f"not reachable on {host}:{port} ({exc.reason})", file=sys.stderr)
        return 1


def _receipt_verify(args: argparse.Namespace) -> int:
    import json

    from app.audit_verify import VerificationError, verify_bundle_file

    try:
        report = verify_bundle_file(
            args.bundle,
            args.public_key,
            expected_key_id=args.key_id,
            expected_fingerprint=args.fingerprint,
        )
    except VerificationError as exc:
        if args.json:
            print(json.dumps({"verified": False, "error": str(exc)}, separators=(",", ":")))
        else:
            print(f"verification failed — {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(report.to_dict(), separators=(",", ":")))
    else:
        print("verified")
        print(f"  receipt:    {report.receipt_id}")
        print(f"  decision:   {report.decision_id}")
        print(f"  checkpoint: {report.checkpoint_id}")
        print(f"  sequence:   {report.evidence_sequence}")
        print(f"  key:        {report.key_id}")
        print(f"  fingerprint:{report.public_key_fingerprint}")
        print(f"  claim:      {report.claim}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="nautgate", description="NautGate LLM gateway")
    parser.add_argument("--version", action="version", version=f"nautgate {get_version()}")
    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="run the gateway")
    serve.add_argument("--host", help=f"bind address (default {DEFAULT_HOST})")
    serve.add_argument("--port", type=int, help=f"port (default {DEFAULT_PORT})")
    serve.add_argument("--reload", action="store_true", help="reload on code changes")
    serve.set_defaults(func=_serve)

    status = sub.add_parser("status", help="check whether the gateway is responding")
    status.add_argument("--host", help=f"address to probe (default {DEFAULT_HOST})")
    status.add_argument("--port", type=int, help=f"port to probe (default {DEFAULT_PORT})")
    status.set_defaults(func=_status)

    receipt = sub.add_parser("receipt", help="work with Verified Audit Trail receipts")
    receipt_sub = receipt.add_subparsers(dest="receipt_command")
    verify = receipt_sub.add_parser("verify", help="verify an evidence bundle offline")
    verify.add_argument("bundle", help="path to evidence-bundle JSON")
    verify.add_argument("--public-key", required=True, help="trusted PEM public key or certificate")
    verify.add_argument("--key-id", help="required signing key ID")
    verify.add_argument("--fingerprint", help="required SHA-256 public-key fingerprint")
    verify.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    verify.set_defaults(func=_receipt_verify)

    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 1
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
