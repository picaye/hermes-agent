"""A linked worktree boots on its checkout's committed dependency env (no per-worktree install).

Fresh worktree lanes of a Hermes checkout used to compute their own install key (the path hash),
found no ``installs/<key>/facts.json``, and died in the pre-import gate with
``NoCommittedEnvironment`` until someone ran ``hermes pm repair`` for the throwaway key — every
new worktree card on the kanban dispatcher crashed in a loop. Keying is now folded onto the
canonical checkout via ``canonical_checkout_root``; these tests pin the gitfile parsing (real
layouts, plus the layouts that must NOT be folded) and the spawn-gate outcome end-to-end.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from pm import environments

pytestmark = pytest.mark.platforms("any")


@pytest.fixture(autouse=True)
def _isolated_installs_root(tmp_path, monkeypatch):
    monkeypatch.setattr(environments, "installs_root", lambda: tmp_path / "installs")


def _make_checkout(tmp_path: Path, name: str = "hermes-agent") -> Path:
    checkout = tmp_path / name
    (checkout / ".git").mkdir(parents=True)
    (checkout / "hermes_bootstrap.py").write_text("", encoding="utf-8")
    (checkout / "uv.lock").write_text("", encoding="utf-8")
    (checkout / "pyproject.toml").write_text("", encoding="utf-8")
    return checkout


def _make_linked_worktree(checkout: Path, name: str = "wt-one", *, relative: bool = False) -> Path:
    """Lay out exactly what ``git worktree add`` writes: a gitfile + ``commondir``."""
    import os

    worktree = checkout.parent / f"{checkout.name}.worktrees" / name
    gitdir = checkout / ".git" / "worktrees" / name
    gitdir.mkdir(parents=True, exist_ok=True)
    worktree.mkdir(parents=True)
    (worktree / "hermes_bootstrap.py").write_text("", encoding="utf-8")
    target = os.path.relpath(gitdir, worktree) if relative else str(gitdir)
    (worktree / ".git").write_text(f"gitdir: {target}\n", encoding="utf-8")
    (gitdir / "commondir").write_text("../..\n", encoding="utf-8")
    (gitdir / "gitdir").write_text(str(worktree / ".git") + "\n", encoding="utf-8")
    return worktree


def test_plain_checkout_key_is_unchanged_path_hash(tmp_path):
    """Existing commits keep resolving: the canonical checkout folds to itself."""
    import hashlib

    checkout = _make_checkout(tmp_path)
    expected = hashlib.sha256(str(checkout.resolve()).encode("utf-8")).hexdigest()[:16]
    assert environments.install_key(checkout) == expected


def test_linked_worktree_shares_the_checkout_install_key(tmp_path):
    checkout = _make_checkout(tmp_path)
    worktree = _make_linked_worktree(checkout)
    assert environments.canonical_checkout_root(worktree) == checkout.resolve()
    assert environments.install_key(worktree) == environments.install_key(checkout)


def test_relative_gitdir_pointer_is_followed(tmp_path):
    checkout = _make_checkout(tmp_path)
    worktree = _make_linked_worktree(checkout, relative=True)
    assert environments.install_key(worktree) == environments.install_key(checkout)


def test_main_checkout_key_is_stable_across_new_worktrees(tmp_path):
    """One install state per repo however many campaign worktrees come and go."""
    checkout = _make_checkout(tmp_path)
    keys = {environments.install_key(_make_linked_worktree(checkout, name=f"w{i}")) for i in range(3)}
    assert keys == {environments.install_key(checkout)}


def test_submodule_layout_keeps_its_own_key(tmp_path):
    """A submodule gitdir IS the common dir and carries no ``commondir`` file."""
    checkout = _make_checkout(tmp_path)
    submodule = tmp_path / "vendor-lib"
    gitdir = checkout / ".git" / "modules" / "vendor-lib"
    gitdir.mkdir(parents=True)
    submodule.mkdir()
    (submodule / ".git").write_text(f"gitdir: {gitdir}\n", encoding="utf-8")
    assert environments.canonical_checkout_root(submodule) == submodule.resolve()


def test_non_hermes_worktree_is_not_folded(tmp_path):
    """Shared code must not hijack other repos' worktree identity."""
    other = tmp_path / "other-repo"
    (other / ".git").mkdir(parents=True)
    worktree = tmp_path / "other-repo-wt"
    gitdir = other / ".git" / "worktrees" / "feature"
    gitdir.mkdir(parents=True)
    worktree.mkdir()
    (worktree / ".git").write_text(f"gitdir: {gitdir}\n", encoding="utf-8")
    (gitdir / "commondir").write_text("../..\n", encoding="utf-8")
    assert environments.canonical_checkout_root(worktree) == worktree.resolve()


def test_moved_or_deleted_main_checkout_keeps_worktree_own_key(tmp_path):
    """No exception and no fold to a phantom dir: the old repair hint path stays valid."""
    checkout = _make_checkout(tmp_path)
    worktree = _make_linked_worktree(checkout)
    assert environments.install_key(worktree) == environments.install_key(checkout)
    (checkout / "hermes_bootstrap.py").unlink()  # main moved away or stopped being Hermes
    assert environments.canonical_checkout_root(worktree) == worktree.resolve()


def test_fresh_worktree_boots_on_the_checkouts_committed_env(tmp_path):
    """The exact spawn gate from the incident: worktree with NO state of its own."""
    checkout = _make_checkout(tmp_path)
    worktree = _make_linked_worktree(checkout)

    state = environments.install_state_dir(checkout)
    generation = state / "environments" / "gen00001"
    venv = generation / "venv"
    (venv / "lib" / "python3.11" / "site-packages").mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text("home = /usr/bin\n", encoding="utf-8")
    (state / "inputs").mkdir(parents=True)
    (state / "facts.json").write_text(
        json.dumps({"packages": {"venv": {"environment": str(venv)}}}), encoding="utf-8"
    )

    # Pre-fix this is where a worktree lane died: its own key had no state dir at all.
    assert environments.install_state_dir(worktree) == state
    assert environments.committed_venv(worktree) == venv.resolve()


def test_record_activation_inputs_stamps_the_canonical_root(tmp_path):
    """GC reads the stamp as 'is the checkout gone' — a disposable worktree must never be named."""
    checkout = _make_checkout(tmp_path)
    worktree = _make_linked_worktree(checkout)
    environments.record_activation_inputs(
        environments.activation_inputs_dir(worktree),
        {"uv.lock": 1, "pyproject.toml": 1, "uv.extra_requirements": 1},
        worktree,
        test_environment=False,
    )
    stamps = environments.activation_inputs_dir(worktree)
    assert (stamps / ".project-root").read_text(encoding="utf-8") == str(checkout.resolve())
