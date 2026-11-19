"""Fresh Hermes-checkout worktree lanes get a committed PM env at creation.

Without provisioning, the first worker spawn on a brand-new
``workspace_kind=worktree`` card crash-loops: a linked worktree has its own
PM install key (sha256 of its resolved path) with no
``~/.hermes/installs/<key>/facts.json``, so ``activate_dependencies`` raises
``NoCommittedEnvironment`` until a human runs ``pm repair`` by hand
(t_39461905; the cto chose per-lane provisioning inside
``_ensure_git_worktree`` over a spawn-path shared-env fallback).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from hermes_cli import kanban_db_workspace as kbw


def _git(*args: str, cwd: str | None = None) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, f"git {' '.join(args)} failed: {result.stderr}"
    return result.stdout


def _make_repo(tmp_path: Path, *, hermes: bool) -> Path:
    repo = tmp_path / ("hermes-agent" if hermes else "plain-project")
    _git("init", str(repo))
    if hermes:
        (repo / "hermes_bootstrap.py").write_text("", encoding="utf-8")
        (repo / "pyproject.toml").write_text(
            '[project]\nname = "hermes-agent"\nversion = "0"\n', encoding="utf-8"
        )
    (repo / "README.md").write_text("x\n", encoding="utf-8")
    _git("add", "-A", cwd=str(repo))
    _git(
        "-c", "user.email=tests@example.invalid", "-c", "user.name=tests",
        "commit", "-m", "init", cwd=str(repo),
    )
    return repo


@pytest.fixture
def hermes_repo(tmp_path: Path) -> Path:
    return _make_repo(tmp_path, hermes=True)


@pytest.fixture
def plain_repo(tmp_path: Path) -> Path:
    return _make_repo(tmp_path, hermes=False)


@pytest.fixture
def provision_calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[list[str], str | None]]:
    """Intercept the ``pm repair`` provisioning subprocess, pass git through.

    The provisioning call is recognized by its ``-m hermes_cli.main pm``
    argv head, so the real ``git worktree add`` machinery still executes.
    """
    calls: list[tuple[list[str], str | None]] = []
    real_run = subprocess.run

    def fake_run(argv, **kwargs):
        if isinstance(argv, list) and argv[1:4] == ["-m", "hermes_cli.main", "pm"]:
            calls.append((argv, kwargs.get("cwd")))
            return subprocess.CompletedProcess(argv, 0, "", "")
        return real_run(argv, **kwargs)

    monkeypatch.setattr(subprocess, "run", fake_run)
    return calls


def test_fresh_hermes_lane_is_provisioned_at_the_lane_root(hermes_repo: Path, provision_calls):
    target = hermes_repo / ".worktrees" / "t_new"
    kbw._ensure_git_worktree(hermes_repo, target, "wt/t_new")

    assert len(provision_calls) == 1, "fresh Hermes lane must provision exactly once"
    argv, cwd = provision_calls[0]
    assert argv[0] == sys.executable, "provisioning must ride the same interpreter as the spawner"
    assert argv[1:] == ["-m", "hermes_cli.main", "pm", "repair"]
    assert cwd == str(target), (
        "cwd must be the worktree so PM resolves the lane's own install key "
        "(repo_root() is the imported pm's checkout)"
    )
    assert (target / "hermes_bootstrap.py").is_file(), "the lane itself must still materialize"


def test_existing_lane_is_not_reprovisioned(hermes_repo: Path, provision_calls):
    target = hermes_repo / ".worktrees" / "t_re"
    kbw._ensure_git_worktree(hermes_repo, target, "wt/t_re")
    kbw._ensure_git_worktree(hermes_repo, target, "wt/t_re")
    assert len(provision_calls) == 1, "re-claim of an existing lane must not rebuild its env"


def test_plain_repo_lane_gets_no_pm_call(plain_repo: Path, provision_calls):
    target = plain_repo / ".worktrees" / "t_plain"
    kbw._ensure_git_worktree(plain_repo, target, "wt/t_plain")
    assert provision_calls == []
    assert (target / "README.md").is_file(), "ordinary worktree lanes keep working"


def test_provision_failure_never_fails_the_claim(hermes_repo: Path, monkeypatch: pytest.MonkeyPatch):
    real_run = subprocess.run

    def failing_run(argv, **kwargs):
        if isinstance(argv, list) and argv[1:4] == ["-m", "hermes_cli.main", "pm"]:
            return subprocess.CompletedProcess(argv, 1, "", "boom: no network")
        return real_run(argv, **kwargs)

    monkeypatch.setattr(subprocess, "run", failing_run)
    target = hermes_repo / ".worktrees" / "t_fail"
    kbw._ensure_git_worktree(hermes_repo, target, "wt/t_fail")
    # The lane exists and the claim survived; the boot-time recovery retry
    # budget (.repair-incomplete marker accounting) gets the next chance.
    assert (target / "hermes_bootstrap.py").is_file()


def test_provision_timeout_never_fails_the_claim(hermes_repo: Path, monkeypatch: pytest.MonkeyPatch):
    real_run = subprocess.run

    def hanging_run(argv, **kwargs):
        if isinstance(argv, list) and argv[1:4] == ["-m", "hermes_cli.main", "pm"]:
            raise subprocess.TimeoutExpired(argv, kwargs.get("timeout", 0))
        return real_run(argv, **kwargs)

    monkeypatch.setattr(subprocess, "run", hanging_run)
    target = hermes_repo / ".worktrees" / "t_hang"
    kbw._ensure_git_worktree(hermes_repo, target, "wt/t_hang")
    assert (target / "hermes_bootstrap.py").is_file()


def test_worktree_kind_repo_root_anchor_provisions(tmp_path: Path, provision_calls):
    """The claim-time entry point (not just the helper) reaches provisioning."""

    class _Task:
        id = "t_anchor"
        branch_name = ""
        workspace_path = ""

    repo = _make_repo(tmp_path, hermes=True)
    # No workspace_path -> board default_workdir anchor path; exercise the
    # anchored helper directly, which is what resolve_workspace dispatches to.
    target, branch = kbw._anchored_worktree(repo, _Task.id, "wt/t_anchor")
    assert len(provision_calls) == 1
    assert provision_calls[0][1] == str(target)
    assert branch == "wt/t_anchor"
