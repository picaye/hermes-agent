"""Post-deploy guard (t_c1069542): a detached upstream-tag checkout must not drop fork fixes.

The Oct-15 deploy moved the live checkout to detached upstream tag d923a5eb4 (Release
0.22.2) and silently dropped the designated cron fix e623e8575e. These tests replay that
failure on a scratch repo: checkout at a detached upstream tag lacking a designated fork
fix -> the guard flags the detached tag, names the dropped fix, logs the verdict to the
deploy report, and the CLI exits non-zero.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from hermes_cli import deploy_guard


def _env(home: Path) -> dict:
    return {
        **os.environ,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "HOME": str(home),
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.invalid",
    }


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=_env(cwd),
    ).stdout.strip()


@pytest.fixture()
def incident_repo(tmp_path, monkeypatch):
    """A fork clone whose main carries a designated fix, plus an upstream release tag without it.

    Returns (checkout, tag_sha, fix_sha, registry_path): the scratch checkout is left on
    ``main``; the test moves it to the tag to replay the deploy failure.
    """
    for name, value in _env(tmp_path).items():
        if name.startswith("GIT_") or name == "HOME":
            monkeypatch.setenv(name, value)
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    _git(upstream, "init", "-q", "-b", "main")
    (upstream / "f.txt").write_text("0\n", encoding="utf-8")
    _git(upstream, "add", "f.txt")
    _git(upstream, "commit", "-qm", "Release 0.22.2")
    tag_sha = _git(upstream, "rev-parse", "HEAD")
    _git(upstream, "tag", "v0.22.2")
    checkout = tmp_path / "checkout"
    _git(tmp_path, "clone", "-q", str(upstream), str(checkout))
    _git(checkout, "remote", "add", "upstream", str(upstream))
    # The fork fix: a commit on main that no upstream tag contains.
    (checkout / "fix.txt").write_text("cron false-FAILED fix\n", encoding="utf-8")
    _git(checkout, "add", "fix.txt")
    _git(
        checkout,
        "commit",
        "-qm",
        "fix(cron): a self-removing job records completed, not failed",
    )
    fix_sha = _git(checkout, "rev-parse", "HEAD")
    registry = tmp_path / "fork_fixes.json"
    registry.write_text(
        json.dumps(
            {"designated_fixes": [{"sha": fix_sha, "why": "cron false-FAILED fix"}]}
        ),
        encoding="utf-8",
    )
    return checkout, tag_sha, fix_sha, registry


def test_detached_upstream_tag_checkout_fails_the_guard(incident_repo):
    checkout, tag_sha, fix_sha, registry = incident_repo
    _git(checkout, "checkout", "-q", "v0.22.2")  # the Oct-15 updater move
    verdict = deploy_guard.run_guard(repo_root=checkout, registry=registry)
    assert verdict["ok"] is False
    assert verdict["detached_tag"] is True and verdict["tag"] == "v0.22.2"
    assert verdict["head_sha"] == tag_sha
    assert verdict["missing_from_head"] == [fix_sha]
    assert any("is NOT an ancestor of checkout HEAD" in f for f in verdict["findings"])
    assert any("detached upstream tag" in f for f in verdict["findings"])


def test_guard_cli_exits_nonzero_on_the_dropped_fix(incident_repo, capsys):
    checkout, _tag, _fix, registry = incident_repo
    _git(checkout, "checkout", "-q", "v0.22.2")
    rc = deploy_guard.main(
        [
            "--repo-root",
            str(checkout),
            "--registry",
            str(registry),
            "--report-home",
            str(checkout.parent / "home"),
        ]
    )
    assert rc == 1
    out = capsys.readouterr().out
    assert "deploy guard: FAIL" in out
    assert "designated fork fix" in out


def test_verdict_is_logged_to_the_deploy_report(incident_repo):
    checkout, _tag, fix_sha, registry = incident_repo
    home = checkout.parent / "home"
    _git(checkout, "checkout", "-q", "v0.22.2")
    verdict = deploy_guard.guard_after_checkout(checkout, registry=registry, home=home)
    assert verdict["ok"] is False
    line = (home / "deploy_guard.jsonl").read_text(encoding="utf-8").strip()
    logged = json.loads(line)
    assert logged["missing_from_head"] == [fix_sha]
    assert logged["detached_tag"] is True


def test_main_checkout_passes_the_guard(incident_repo):
    checkout, _tag, _fix, registry = incident_repo  # already on main
    verdict = deploy_guard.run_guard(repo_root=checkout, registry=registry)
    assert verdict["ok"] is True, verdict["findings"]
    assert verdict["branch"] == "main"
    assert verdict["missing_from_head"] == []
    rc = deploy_guard.main(
        [
            "--repo-root",
            str(checkout),
            "--registry",
            str(registry),
            "--report-home",
            str(checkout.parent / "home"),
        ]
    )
    assert rc == 0


def test_stale_gateway_code_sha_is_flagged_even_when_head_is_fixed(incident_repo):
    """HEAD is on main (fix present) but the running gateway still boots the tag commit."""
    checkout, tag_sha, fix_sha, registry = incident_repo
    gw_state = checkout.parent / "gateway_state.json"
    gw_state.write_text(json.dumps({"code_sha": tag_sha}), encoding="utf-8")
    verdict = deploy_guard.run_guard(
        repo_root=checkout, registry=registry, gateway_state=gw_state
    )
    assert verdict["ok"] is False
    assert verdict["missing_from_head"] == []
    assert verdict["missing_from_code_sha"] == [fix_sha]
    assert any("running gateway code_sha" in f for f in verdict["findings"])


def test_missing_registry_is_a_noop(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "f.txt").write_text("0\n", encoding="utf-8")
    _git(repo, "add", "f.txt")
    _git(repo, "commit", "-qm", "c0")
    verdict = deploy_guard.run_guard(
        repo_root=repo, registry=tmp_path / "does-not-exist.json"
    )
    assert verdict["ok"] is True and verdict["designated_fixes"] == []


def test_the_shipped_registry_covers_the_oct15_dropped_fix():
    """The real fork registry designates the fix the Oct-15 deploy dropped."""
    fixes = {f["sha"] for f in deploy_guard.load_designated_fixes()}
    assert "e623e8575e53f511bf3c181cf6a5b47804532567" in fixes
