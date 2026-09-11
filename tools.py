"""Tool handlers for the kotoba Hermes plugin.

This file contains NO answers about kotoba-lang. It turns tool arguments into
an argv, runs `read_authority.cljk` (or `amu`), and turns an exit code into a
protocol answer. The reasoning about what an authority file means lives beside
the data, in the same language, so there is exactly one reader
(the doctrine of ADR-2608197300 §3, as applied by `dashboard_auth/did`).

## The exit codes are the contract

    0  answered              -> tool_result
    1  answered: not present -> tool_result with found=false
    2  could not answer      -> tool_error

1 and 2 are kept apart deliberately. "this feature is not listed in the
authority" is a measurement. "I could not open the authority" is not, and a
caller that cannot tell them apart will report an unread file as a permanent
absence -- the failure class this workspace names in ADR-2608136000.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from typing import Any, Dict, Optional

from tools.registry import tool_error, tool_result

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
READER = os.path.join(PLUGIN_DIR, "read_authority.cljk")

TIMEOUT_S = 60


def _workspace() -> Optional[str]:
    ws = os.environ.get("KOTOBA_WORKSPACE")
    return os.path.expanduser(ws) if ws else None


def _nbb() -> Optional[str]:
    return os.environ.get("KOTOBA_NBB") or shutil.which("nbb")


def available() -> bool:
    """Gate for tool dispatch: a configured workspace and a reader to run it."""
    ws = _workspace()
    return bool(ws and os.path.isdir(ws) and _nbb() and os.path.exists(READER))


def _run_reader(command: str, argument: str = "") -> str:
    ws = _workspace()
    if not ws:
        return tool_error(
            "KOTOBA_WORKSPACE is not set. This tool reports what the "
            "kotoba-lang authority files say; without a checkout it can "
            "report nothing, and reporting nothing must not read as "
            "'the feature is absent'."
        )
    nbb = _nbb()
    if not nbb:
        return tool_error("nbb was not found (set KOTOBA_NBB or put it on PATH).")

    argv = [nbb, READER, command, ws]
    if argument:
        argv.append(argument)
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=TIMEOUT_S
        )
    except subprocess.TimeoutExpired:
        return tool_error(f"the authority reader did not finish in {TIMEOUT_S}s")
    except OSError as exc:
        return tool_error(f"could not run the authority reader: {exc}")

    payload: Dict[str, Any]
    try:
        payload = json.loads(proc.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        # No parseable answer. Keep the reader's own words: the cause is
        # usually in them (a broken classpath, a missing namespace), and a
        # handler that discards stderr reports a mystery instead.
        detail = (proc.stderr or proc.stdout or "").strip()[:800]
        return tool_error(
            f"the authority reader produced no answer (exit {proc.returncode})",
            detail=detail,
        )

    if proc.returncode == 2:
        return tool_error(
            "could not read the kotoba-lang authority: "
            f"{payload.get('why', 'unknown')}",
            **{k: v for k, v in payload.items() if k != "ok"},
        )
    # 0 and 1 are both answers. The caller distinguishes them by `found`.
    return tool_result(payload)


# ── kotoba_surface_status ──────────────────────────────────────────────────

SURFACE_STATUS_SCHEMA = {
    "name": "kotoba_surface_status",
    "description": (
        "Is a Kotoba restriction PERMANENT or a backend that has not caught "
        "up? Reads lang/surface-status.edn and returns the entry's "
        "`disposition`: intentional-security-constraint and "
        "intentional-semantic-simplification are permanent; "
        "implemented-partial exists already; not-yet-implemented is a GAP, "
        "not a prohibition. Call this before concluding that Kotoba cannot "
        "express something, and before repeating any readiness claim from "
        "memory or from a document -- every such value in this workspace has "
        "gone stale at least once. The answer carries the authority's own "
        "`as-of` date; if it is old, the checkout is behind, not the language."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": (
                    "The invariant / surface entry to look up, e.g. "
                    "'bool-is-a-type-not-a-number'. Omit to list every entry "
                    "with its disposition."
                ),
            }
        },
        "required": [],
    },
}


def handle_surface_status(args: Dict[str, Any]) -> str:
    return _run_reader("surface-status", str(args.get("name") or ""))


# ── kotoba_capability_kits ─────────────────────────────────────────────────

CAPABILITY_KITS_SCHEMA = {
    "name": "kotoba_capability_kits",
    "description": (
        "Which backends have QUALIFIED a Kotoba capability kit. Reads every "
        "resources/kotoba/lang/capability-kits/*.edn and returns each kit's "
        "`qualification` map verbatim. The key set differs per kit, so never "
        "summarise across kits, and `pending` records a measured refusal with "
        "a reason -- it does not mean nobody tried. Reading these with grep "
        "truncates silently on wrapped lines, which is why this tool exists."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "kit": {
                "type": "string",
                "description": "One kit id, e.g. 'ui-v1'. Omit for all kits.",
            }
        },
        "required": [],
    },
}


def handle_capability_kits(args: Dict[str, Any]) -> str:
    return _run_reader("capability-kits", str(args.get("kit") or ""))


# ── kotoba_check ───────────────────────────────────────────────────────────

CHECK_SCHEMA = {
    "name": "kotoba_check",
    "description": (
        "Run `amu check` on one .kotoba file and return its verdict verbatim. "
        "IMPORTANT: `check` returning ok is NOT 'it works'. A value of the "
        "wrong type for document-bool passes check and fails at the value "
        "phase when the export is executed. Treat a passing check as "
        "'the compiler admitted it', and run the export before saying more."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "file": {
                "type": "string",
                "description": "Path to the .kotoba file, relative to KOTOBA_WORKSPACE or absolute.",
            }
        },
        "required": ["file"],
    },
}


def handle_check(args: Dict[str, Any]) -> str:
    ws = _workspace()
    if not ws:
        return tool_error("KOTOBA_WORKSPACE is not set.")
    raw = str(args.get("file") or "").strip()
    if not raw:
        return tool_error("file is required")
    target = raw if os.path.isabs(raw) else os.path.join(ws, raw)
    if not os.path.exists(target):
        return tool_error(f"no such file: {target}")

    amu = os.environ.get("KOTOBA_AMU") or os.path.join(
        ws, "orgs/kotoba-lang/amu/bin/amu"
    )
    if not os.path.exists(amu):
        return tool_error(f"amu was not found at {amu} (set KOTOBA_AMU)")
    try:
        proc = subprocess.run(
            [amu, "check", target],
            capture_output=True,
            text=True,
            timeout=300,
            cwd=ws,
        )
    except subprocess.TimeoutExpired:
        return tool_error("amu check did not finish in 300s")
    except OSError as exc:
        return tool_error(f"could not run amu: {exc}")

    return tool_result(
        {
            "exit": proc.returncode,
            "stdout": proc.stdout[-4000:],
            "stderr": proc.stderr[-2000:],
            "note": (
                "check admitted the module; it did not run it. A "
                "document-bool type error appears only when the export runs."
                if proc.returncode == 0
                else "check refused the module; the reason is in stdout/stderr."
            ),
        }
    )
