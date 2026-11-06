"""Regression: receipt recovery ran compose/deliver with no profile secret scope.

Under multi-profile hosting (``set_multiplex_active(True)``) ``get_secret`` fails
closed for an unscoped read. ``reconcile_pending`` retried every open admission
journal's delivery by calling ``_save_compose_deliver`` directly — on the ticker
thread, outside any fire scope. The delivery chain resolves ``<PLATFORM>_HOME_*``
secrets through ``get_secret`` (``_home_env_lookup``; the fleet incident raised on
the matrix plugin's ``MATRIX_HOME_ROOM`` probe in the ``deliver=origin`` home
fallback). The raise aborted the retry before the receipt was saved, so the
journal never closed and every tick replayed the same failure — the
'cron.scheduler_authority: Cron receipt recovery deferred' + UnscopedSecretError
traceback pair, once per minute, persisting across gateway restarts because the
journal is durable.

The fire path already installs the owning profile's scope before resolving
delivery targets (``_run_one_job_body`` / ``_install_fire_secret_scope``, and the
same wrapper fixes the crash-notice path at ``scheduler._deliver_crash_failure``);
the recovery path must honour the same contract. Mirrors the dashboard-route fix
in tests/hermes_cli/test_cron_delivery_targets_scope.py.
"""
import json

import pytest


@pytest.fixture()
def multiplex():
    from agent.secret_scope import set_multiplex_active
    set_multiplex_active(True)
    try:
        yield
    finally:
        set_multiplex_active(False)


def test_reconcile_pending_binds_profile_scope_for_compose_deliver(
    tmp_path, monkeypatch, multiplex, caplog
):
    from cron import executions, jobs, scheduler_authority
    from hermes_constants import get_hermes_home

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    # The owning profile's home secret; the unscoped read must resolve it, not raise.
    (tmp_path / ".env").write_text("MATRIX_HOME_ROOM=room-12345\n")

    with jobs.use_cron_store(tmp_path / "cron"):
        job = jobs.create_job(prompt="report", schedule="every 1h", deliver="origin")
        row = executions.create_execution(job["id"], source="reconcile-fixture")
        # A row claimed by THIS process counts as live-in-flight and recovery correctly stands
        # down for it; the incident journal's firer had died. Stamp the claim to a nonexistent
        # pid so recovery takes ownership of the tail, as it did on the fleet.
        import sqlite3
        with sqlite3.connect(get_hermes_home() / "cron" / "executions.db") as conn:
            conn.execute("UPDATE executions SET process_id='dead-owner', pid=2147483646, "
                         "process_started_at='1970-01-01T00:00:00' WHERE id=?", (row["id"],))
        params = {"job_id": job["id"], "request_id": row["id"], "extra_prompt": None}
        journal = scheduler_authority.journal_path(job["id"], row["id"])
        journal.parent.mkdir(parents=True, exist_ok=True)
        # 'refusal' shortcut: the recovery loop owns this attempt outright, no live peer.
        journal.write_text(
            json.dumps({"params": params, "refusal": [True, "report", "answer", None],
                        "job": job}),
            encoding="utf-8",
        )

        from agent.secret_scope import UnscopedSecretError, get_secret

        seen: dict = {}

        def _probe_save(d, fence, final_response, output, **kwargs):
            # Same read shape as scheduler_delivery._home_env_lookup during origin
            # target resolution — the exact frame that raised in the incident.
            seen["room"] = get_secret("MATRIX_HOME_ROOM", "")

        monkeypatch.setattr("cron.scheduler._save_compose_deliver", _probe_save)

        with caplog.at_level("WARNING", logger="cron.scheduler_authority"):
            scheduler_authority.reconcile_pending()

        assert seen.get("room") == "room-12345", (
            "recovery must run compose/deliver inside the owning profile's secret scope"
        )
        assert not any("receipt recovery deferred" in r.message for r in caplog.records), (
            "scoped retry must not enter the deferred-recovery loop"
        )
        assert not journal.exists(), "journal must close once the receipt is saved"
