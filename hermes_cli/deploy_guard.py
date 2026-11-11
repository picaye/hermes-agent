"""Post-deploy invariant guard: a detached upstream-tag checkout must not drop fork fixes.

Incident history (picaye/hermes-agent, October 2026): twice in four days the updater moved
the live checkout onto a detached upstream release tag, silently dropping every fork-only fix
from the running code. Oct-12 dropped a PyJWT critical (t_2e600a50); Oct-15 14:24 CEST moved
the checkout to detached ``d923a5eb4`` (upstream Release 0.22.2) and dropped the cron
false-FAILED fix ``e623e8575e`` (repaired after the fact by merge ``8f65f7a0f4``).

This module turns that post-mortem into a checkable invariant, run after any updater
checkout move and at every gateway boot:

(a) a checkout that landed on a detached tag is flagged — the fork's deploy target is
    ``main``; upstream releases must be merged into it, never bare-checked-out;
(b) every designated fork fix listed in ``hermes_cli/fork_fixes.json`` must be an ancestor
    of the new checkout HEAD and of the ``code_sha`` recorded in ``gateway_state.json``.

The verdict is appended to ``<HERMES_HOME>/deploy_guard.jsonl`` (one JSON line per check)
and printed as a warning when it fails. ``guard_after_checkout`` never raises: the guard is
best-effort by design and must not break the updater or the gateway boot. The CLI entry
(``python -m hermes_cli.deploy_guard``) exits non-zero on a failed verdict so probes and CI
can assert on it.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import subprocess
import sys
from datetime import datetime, timezone, UTC
from pathlib import Path

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_HEX_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")


def default_registry_path() -> Path:
    """The fork's designated-fix registry shipped next to this module."""
    return Path(__file__).with_name("fork_fixes.json")


def load_designated_fixes(registry: Path | None = None) -> list[dict]:
    """Designated fix entries ``{"sha", "why"}`` from the registry; [] when absent/invalid.

    A missing or unreadable registry is a no-op, not a failure: upstream checkouts (and
    forks without this file) simply have nothing designated.
    """
    path = Path(registry) if registry else default_registry_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    fixes: list[dict] = []
    entries = data.get("designated_fixes") if isinstance(data, dict) else None
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        sha = str(entry.get("sha", "")).strip().lower()
        if _HEX_SHA_RE.match(sha):
            fixes.append({"sha": sha, "why": str(entry.get("why", ""))})
    return fixes


def _git(git_cmd: list[str], args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    """Run one git command; never raises (missing git surfaces as returncode 127)."""
    try:
        return subprocess.run(
            [*git_cmd, *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        logger.debug("deploy_guard: git %s failed", " ".join(args), exc_info=True)
        return subprocess.CompletedProcess([*git_cmd, *args], 127, "", "")


def _head_state(git_cmd: list[str], repo_root: Path) -> tuple[str | None, str | None]:
    """``(branch, sha)`` — branch is None on a detached HEAD; sha None when not a checkout."""
    branch = _git(git_cmd, ["symbolic-ref", "-q", "--short", "HEAD"], repo_root)
    sha = _git(git_cmd, ["rev-parse", "HEAD"], repo_root)
    if sha.returncode != 0:
        return None, None
    return (
        branch.stdout.strip() if branch.returncode == 0 else None
    ), sha.stdout.strip()


def _is_ancestor(
    git_cmd: list[str], repo_root: Path, ancestor: str, descendant: str
) -> bool | None:
    """True/False ancestry verdict; None when unprovable (object absent, e.g. shallow)."""
    if (
        _git(
            git_cmd, ["cat-file", "-e", f"{ancestor}^{{commit}}"], repo_root
        ).returncode
        != 0
    ):
        return None
    return (
        _git(
            git_cmd, ["merge-base", "--is-ancestor", ancestor, descendant], repo_root
        ).returncode
        == 0
    )


def _gateway_code_sha(path: Path) -> str | None:
    """The ``code_sha`` a running gateway recorded, or None when absent/unreadable."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    sha = (
        str(data.get("code_sha", "")).strip().lower() if isinstance(data, dict) else ""
    )
    return sha if _HEX_SHA_RE.match(sha) else None


def run_guard(
    *,
    repo_root: Path | None = None,
    registry: Path | None = None,
    gateway_state: Path | None = None,
    git_cmd: list[str] | None = None,
) -> dict:
    """Evaluate the post-deploy invariants; returns the verdict dict (never raises)."""
    root = Path(repo_root) if repo_root else _PROJECT_ROOT
    git_cmd = git_cmd or ["git"]
    fixes = load_designated_fixes(registry)
    branch, head_sha = _head_state(git_cmd, root)

    verdict: dict = {
        "checked_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "repo_root": str(root),
        "branch": branch,
        "head_sha": head_sha,
        "detached_tag": False,
        "tag": None,
        "designated_fixes": [f["sha"] for f in fixes],
        "missing_from_head": [],
        "unprovable_from_head": [],
        "code_sha": None,
        "missing_from_code_sha": [],
        "ok": True,
        "findings": [],
    }
    if head_sha is None:
        # Not a git checkout (installed wheel, zip build): nothing to prove, no false alarm.
        verdict["skipped"] = "not a git checkout"
        return verdict

    if branch is None:
        tags = _git(git_cmd, ["tag", "--points-at", "HEAD"], root).stdout.split()
        if tags:
            verdict["detached_tag"] = True
            verdict["tag"] = tags[0]
            verdict["findings"].append(
                f"checkout landed on detached upstream tag {tags[0]} ({head_sha[:10]}) — "
                "the fork's deploy target is main; merge the release into main instead of "
                "bare-checking-out the tag"
            )

    gw_path = Path(gateway_state) if gateway_state else None
    if gw_path is None:
        try:
            from hermes_constants import get_hermes_home

            gw_path = get_hermes_home() / "gateway_state.json"
        except Exception:
            gw_path = None
    code_sha = _gateway_code_sha(gw_path) if gw_path else None
    verdict["gateway_state_path"] = str(gw_path) if gw_path else None
    verdict["code_sha"] = code_sha

    for fix in fixes:
        sha = fix["sha"]
        at_head = _is_ancestor(git_cmd, root, sha, head_sha)
        if at_head is False:
            verdict["missing_from_head"].append(sha)
            verdict["findings"].append(
                f"designated fork fix {sha[:10]} ({fix['why']}) is NOT an ancestor of "
                f"checkout HEAD {head_sha[:10]} — the deploy dropped it"
            )
        elif at_head is None:
            verdict["unprovable_from_head"].append(sha)
            verdict["findings"].append(
                f"designated fork fix {sha[:10]} cannot be proven in this checkout "
                "(object absent — shallow history?); fetch full commit graph"
            )
        if (
            code_sha
            and code_sha != head_sha
            and _is_ancestor(git_cmd, root, sha, code_sha) is False
        ):
            verdict["missing_from_code_sha"].append(sha)
            verdict["findings"].append(
                f"designated fork fix {sha[:10]} ({fix['why']}) is NOT an ancestor of the "
                f"running gateway code_sha {code_sha[:10]} — recycle the gateway onto main"
            )

    verdict["ok"] = not verdict["findings"]
    return verdict


def report_path(home: Path | None = None) -> Path:
    """The deploy report: one JSONL line per guard check."""
    if home is None:
        from hermes_constants import get_hermes_home

        home = get_hermes_home()
    return Path(home) / "deploy_guard.jsonl"


def log_verdict(verdict: dict, home: Path | None = None) -> None:
    """Append the verdict to the deploy report. Never raises — a logger bug must not
    break a deploy; the in-memory verdict is still returned to the caller."""
    try:
        path = report_path(home)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(verdict, ensure_ascii=False) + "\n")
    except OSError:
        logger.debug("deploy_guard: report write failed", exc_info=True)


def guard_after_checkout(
    repo_root: Path | None = None,
    *,
    registry: Path | None = None,
    gateway_state: Path | None = None,
    home: Path | None = None,
) -> dict:
    """Run + log the guard after an updater checkout move or a gateway boot. Never raises.

    Prints the findings to stderr so the operator sees them in the update output / gateway
    log; the durable record is ``deploy_guard.jsonl``.
    """
    try:
        verdict = run_guard(
            repo_root=repo_root, registry=registry, gateway_state=gateway_state
        )
    except (
        Exception
    ) as exc:  # health: allow BLE001 -- a guard bug must never break a deploy
        logger.debug("deploy_guard: check failed", exc_info=True)
        return {"ok": True, "skipped": f"guard error: {exc}"}
    log_verdict(verdict, home=home)
    if not verdict.get("ok"):
        for finding in verdict.get("findings", []):
            print(f"  ⚠ POST-DEPLOY GUARD: {finding}", file=sys.stderr)
    return verdict


def main(argv: list[str] | None = None) -> int:
    """CLI entry: exit 0 when the invariants hold, 1 on a dropped-fix/detached-tag verdict."""
    parser = argparse.ArgumentParser(
        prog="hermes_cli.deploy_guard",
        description="Assert designated fork fixes survive the current checkout + gateway code_sha",
    )
    parser.add_argument(
        "--repo-root", default=None, help="checkout to inspect (default: this install)"
    )
    parser.add_argument("--registry", default=None, help="designated-fix registry JSON")
    parser.add_argument(
        "--gateway-state", default=None, help="gateway_state.json to read code_sha from"
    )
    parser.add_argument(
        "--report-home", default=None, help="HERMES_HOME for deploy_guard.jsonl"
    )
    parser.add_argument(
        "--json", action="store_true", help="print the full verdict as JSON"
    )
    args = parser.parse_args(argv)
    verdict = run_guard(
        repo_root=args.repo_root,
        registry=args.registry,
        gateway_state=args.gateway_state,
    )
    log_verdict(verdict, home=Path(args.report_home) if args.report_home else None)
    if args.json:
        print(json.dumps(verdict, indent=2, ensure_ascii=False))
    else:
        print("deploy guard: PASS" if verdict.get("ok") else "deploy guard: FAIL")
        for finding in verdict.get("findings", []):
            print(f"  - {finding}")
    return 0 if verdict.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
