"""A gateway this updater runs INSIDE (cron update in the gateway tree, #100179) accepted a
self-restart request and can only restart after the updater exits — so at fleet-matrix time it is
still on the pre-update code by construction. It used to render STALE and exit 1 on every nightly
cron update (#119597). Now the ancestor pid set recorded by the restart phase turns that row into
``restart_pending``; every OTHER gateway on the old sha keeps its ``stale`` verdict.
"""

import contextlib
import io
import json
import os

import pytest

import hermes_cli.update_cmd_fleet as fleet_mod
import hermes_cli.update_cmd_fleet_verify as fleet_verify
import hermes_cli.update_receipt as ur

OLD = "a" * 40
HEAD = "b" * 40


def _fleet_homes(monkeypatch, tmp_path, records: dict[str, dict]) -> None:
    """Default home + named profiles, each with a state file the identity resolver verifies as-is."""
    root = tmp_path / ".hermes"
    (root / "profiles").mkdir(parents=True)
    homes = {"default": root}
    for profile, record in records.items():
        home = root if profile == "default" else root / "profiles" / profile
        home.mkdir(exist_ok=True)
        homes[profile] = home
        (home / "gateway_state.json").write_text(json.dumps(record), encoding="utf-8")
    by_home = {str(home): rec["pid"] for profile, home in homes.items() for rec in [records[profile]]}
    monkeypatch.setattr("hermes_cli.version_info.get_code_identity", lambda refresh=False: {"sha": HEAD, "version": "1.0"})
    monkeypatch.setattr("hermes_cli.profiles._get_default_hermes_home", lambda: root)
    monkeypatch.setattr("hermes_cli.profiles._get_profiles_root", lambda: root / "profiles")
    monkeypatch.setattr("gateway.control_socket.identify_gateway", lambda h, **k: None)
    monkeypatch.setattr("gateway.status.live_gateway_pid_for_home", lambda h: by_home.get(str(h)))
    monkeypatch.setattr(ur, "_gateway_code_root", lambda pid, home: None)


def test_ancestor_with_accepted_self_restart_is_pending_while_other_stale_rows_still_fail(monkeypatch, tmp_path):
    ancestor, sibling = os.getpid(), os.getppid()
    _fleet_homes(monkeypatch, tmp_path, {
        "default": {"pid": ancestor, "gateway_state": "running", "code_sha": OLD},
        "ops": {"pid": sibling, "gateway_state": "running", "code_sha": OLD},
    })

    fleet = ur.collect_fleet_versions(pre_restart_pids=[ancestor, sibling], self_restart_pending={ancestor})
    states = {row["profile"]: row["state"] for row in fleet}
    assert states == {"default": ur.RESTART_PENDING_STATE, "ops": "stale"}
    with contextlib.redirect_stdout(io.StringIO()) as out:
        assert ur.print_fleet_version_matrix(fleet) is True  # the sibling still fails the update
    assert "restart pending" in out.getvalue() and "1 gateway(s) still running the old code" in out.getvalue()

    # Without the sibling, the pending row alone is not a failure — and the same pid NOT recorded
    # as pending (the restart phase never reached the ancestor branch) is a plain stale verdict.
    only_ancestor = [row for row in fleet if row["profile"] == "default"]
    with contextlib.redirect_stdout(io.StringIO()):
        assert ur.print_fleet_version_matrix(only_ancestor) is False
    plain = ur.collect_fleet_versions(pre_restart_pids=[ancestor, sibling])
    assert {row["state"] for row in plain} == {"stale"}


def test_restart_phase_records_accepted_self_restart_and_verify_keeps_it_owed(monkeypatch, tmp_path):
    """The ancestor branch of the drain triage feeds the pid set the matrix reads. End to end the
    verify phase exits 0 (contract C3) but keeps the restart OWED: a signalled drain is not a
    completed restart, so the follow-up says "signalled, draining", the pending marker is NOT
    cleared, and the armed obligation verifies the successor's code identity on every later CLI
    start (the Oct-11 class: the receipt claimed "restarted", the follow-up claimed "stopped").
    """
    from hermes_cli import gateway
    from hermes_cli import update_cmd

    ancestor = os.getpid()
    monkeypatch.setattr(gateway, "_is_pid_ancestor_of_current_process", lambda pid: pid == ancestor)
    monkeypatch.setattr(gateway, "_request_gateway_self_restart", lambda pid: True)
    pending: set = set()
    with contextlib.redirect_stdout(io.StringIO()):
        assert fleet_mod._drain_or_signal_gateway_for_update(ancestor, 5.0, "default", self_restart_pending=pending)
    assert pending == {ancestor}

    _fleet_homes(monkeypatch, tmp_path, {"default": {"pid": ancestor, "gateway_state": "running", "code_sha": OLD}})
    monkeypatch.setattr(fleet_verify, "_print_legacy_units_warning", lambda: None)
    monkeypatch.setattr("hermes_cli.update_cmd_maint._refresh_dashboard_after_update", lambda *a, **k: None)
    monkeypatch.setattr(update_cmd, "_surviving_pre_update_serve_runtimes", lambda plan: [])
    monkeypatch.setattr(fleet_mod._time, "sleep", lambda s: None)
    cleared = []
    monkeypatch.setattr(fleet_mod, "_clear_fleet_restart_pending_marker", lambda: cleared.append(True))
    monkeypatch.setattr("hermes_cli.gateway_migrate.maybe_auto_migrate_after_update", lambda: None)
    restart = fleet_mod._GatewayRestartOutcome(
        # The restart phase records the label as SIGNALED, never as restarted, and marks the
        # restart owed until the successor is verified.
        incomplete=True, phase_errors=[], pre_restart_gateway_pids=[ancestor], restarted_services=[],
        failed_or_stale_units=[], relaunched_profiles=[], externally_supervised_profiles=[], killed_pids=set(),
        self_restart_pending_pids=pending, restart_signalled_services=["hermes-gateway"],
    )
    with contextlib.redirect_stdout(io.StringIO()) as out:
        fleet_verify._verify_fleet_after_update(
            restart, _pre_update_plan=None, _windows_gateway_resume=None, update_complete=True,
        )  # a SystemExit(1) here is the #119597 symptom
    text = out.getvalue()
    assert "restart pending" in text
    assert "Update not complete" not in text
    assert "follow-up 'gateway_restart'" in text
    # Truthful at both ends: never "stopped" for a live draining pid, never "restarted" for a
    # restart that has not been observed.
    assert "draining" in text and "stopped PIDs" not in text
    assert cleared == []  # the obligation stays armed until the successor proves the new code
    assert restart.incomplete


def test_verify_escalates_restart_pending_rows_even_if_the_restart_phase_left_incomplete_false(monkeypatch, tmp_path):
    """Defense in depth: a fleet matrix row rendered ``restart_pending`` is an UNVERIFIED restart.
    Even when the restart-phase bookkeeping forgot to mark the outcome incomplete, verification
    keeps the debt armed instead of clearing the marker on a false-success reading."""
    from hermes_cli import update_cmd

    ancestor = os.getpid()
    _fleet_homes(monkeypatch, tmp_path, {"default": {"pid": ancestor, "gateway_state": "running", "code_sha": OLD}})
    monkeypatch.setattr(fleet_verify, "_print_legacy_units_warning", lambda: None)
    monkeypatch.setattr("hermes_cli.update_cmd_maint._refresh_dashboard_after_update", lambda *a, **k: None)
    monkeypatch.setattr(update_cmd, "_surviving_pre_update_serve_runtimes", lambda plan: [])
    monkeypatch.setattr(fleet_mod._time, "sleep", lambda s: None)
    cleared = []
    monkeypatch.setattr(fleet_mod, "_clear_fleet_restart_pending_marker", lambda: cleared.append(True))
    monkeypatch.setattr("hermes_cli.gateway_migrate.maybe_auto_migrate_after_update", lambda: None)
    restart = fleet_mod._GatewayRestartOutcome(
        incomplete=False, phase_errors=[], pre_restart_gateway_pids=[ancestor], restarted_services=[],
        failed_or_stale_units=[], relaunched_profiles=[], externally_supervised_profiles=[], killed_pids=set(),
        self_restart_pending_pids={ancestor},
    )
    with contextlib.redirect_stdout(io.StringIO()) as out:
        fleet_verify._verify_fleet_after_update(
            restart, _pre_update_plan=None, _windows_gateway_resume=None, update_complete=True,
        )
    assert restart.incomplete
    assert "draining" in out.getvalue() and "stopped PIDs" not in out.getvalue()
    assert cleared == []


def test_supervisor_wrapper_pid_credits_the_gateway_process_under_it(monkeypatch, tmp_path):
    """launchd/systemd bookkeeping records the supervised wrapper PID while the fleet row carries
    the gateway process underneath it. The wrapper's drain signal restarts the whole tree, so the
    child gateway is equally ``restart_pending`` — judging it STALE is what made the updater
    signal its own live ancestor and then report it as 'stopped' (Oct-11 receipt class)."""
    me, parent = os.getpid(), os.getppid()
    _fleet_homes(monkeypatch, tmp_path, {"default": {"pid": me, "gateway_state": "running", "code_sha": OLD}})
    fleet = ur.collect_fleet_versions(pre_restart_pids=[me], self_restart_pending={parent})
    assert fleet and fleet[0]["state"] == ur.RESTART_PENDING_STATE
    # An unrelated ancestor set still leaves the row STALE: the exemption is per-tree, not global.
    other = ur.collect_fleet_versions(pre_restart_pids=[me], self_restart_pending={parent * 0 + 2 ** 30})
    assert other and other[0]["state"] == "stale"


def test_systemd_ancestor_unit_is_signalled_not_credited(monkeypatch, capsys):
    """#100179 systemd twin: the unit's MainPID is an ancestor of this updater. The unit stays
    ``active`` while the gateway drains after this process exits, so the ``is-active`` polls in
    the graceful path would trivially "confirm" a restart that has not happened. The unit is
    recorded as signalled — never restarted, never failed — and no start/reset-failed verbs are
    issued against a unit we deliberately left alone."""
    import subprocess as sp

    ancestor = 4242
    monkeypatch.setattr(fleet_mod, "_systemctl", lambda cmd, *, timeout: sp.CompletedProcess(
        cmd, 0,
        stdout=(
            "active\n" if "is-active" in " ".join(cmd)
            else f"{ancestor}\n" if "MainPID" in " ".join(cmd) else ""
        ),
        stderr=""))
    monkeypatch.setattr(fleet_mod, "_systemd_unit_owned_by_update", lambda *a, **k: True)
    monkeypatch.setattr(fleet_mod, "_repair_unit_without_fatal_exit_park", lambda *a, **k: None)
    monkeypatch.setattr(fleet_mod, "_service_unit_supports_graceful_sigusr1_restart", lambda name: True)
    monkeypatch.setattr(fleet_mod, "_resolve_manage_cmd", lambda *a, **k: None)
    from hermes_cli import gateway as gw
    monkeypatch.setattr(gw, "_is_pid_ancestor_of_current_process", lambda pid: pid == ancestor)
    monkeypatch.setattr(gw, "_request_gateway_self_restart", lambda pid: True)

    restarted: list = []
    failed: list = []
    signalled: list = []
    pending: set = set()
    fleet_mod._restart_one_systemd_gateway_unit(
        "hermes-gateway", scope="user", scope_cmd=["systemctl", "--user"], drain_budget=5.0,
        _manage_cmd_cache={}, restarted_services=restarted, failed_or_stale_units=failed,
        self_restart_pending=pending, restart_signalled_services=signalled,
    )
    assert restarted == [] and failed == []
    assert signalled == ["hermes-gateway"]
    assert pending == {ancestor}
    assert "draining" in capsys.readouterr().out
