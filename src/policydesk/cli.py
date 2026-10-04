"""Command line entry point.

Right now it has one command: `doctor`.

`doctor` is a health check. It loads your settings, prints them with secrets
masked, and tells you whether the app is ready to run. Every project should
have one -- it turns "it doesn't work" into a specific, fixable message.
"""

from __future__ import annotations

import argparse
import sys

from pydantic import ValidationError

from .config import Settings, get_settings
from .logging_setup import configure_logging, get_logger


def cmd_doctor() -> int:
    """Load settings, report status. Returns a process exit code (0 = fine)."""
    try:
        settings: Settings = get_settings()
    except ValidationError as exc:
        # We land here when a required setting is missing or invalid.
        print("Configuration problem. The app did not start.\n", file=sys.stderr)
        for error in exc.errors():
            location = ".".join(str(part) for part in error["loc"]) or "(settings)"
            print(f"  - {location}: {error['msg']}", file=sys.stderr)
        print("\nHint: copy .env.example to .env and fill it in.", file=sys.stderr)
        return 1

    configure_logging(settings)
    log = get_logger("policydesk.doctor")

    print("PolicyDesk configuration")
    print("-" * 46)
    for key, value in settings.safe_summary().items():
        print(f"  {key:<20} {value}")
    print("-" * 46)

    ready = settings.is_test or settings.llm_provider != "openai" or bool(settings.openai_api_key)
    if ready:
        print("Status: READY")
    else:
        print("Status: NOT READY - no model credentials")

    # Proof that redaction works: we deliberately try to log a fake secret.
    log.info("doctor_check_complete", ready=ready, demo_leak="sk-THIS_SHOULD_BE_HIDDEN_12345")
    return 0 if ready else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="policydesk", description="PolicyDesk tools")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor", help="check configuration and report readiness")

    args = parser.parse_args(argv)
    if args.command == "doctor":
        return cmd_doctor()
    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
