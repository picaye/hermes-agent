"""Self-deleting cron jobs must not record a false FAILED despite a delivered receipt.

Sweeps 119-120 evidence: job 09fad1e199cd removed its own definition mid-run via a terminal
``hermes cron remove`` SUBPROCESS. The in-process ``self_removal_delivery_scope`` marker never
armed (different process), so the fire-claim heartbeat could not re-validate the claim (the
record is gone) and the scheduler took the stale-result discard branch
(``_record_fire_ownership_lost``) — writing status='failed' to executions.db even though the
usage_audit receipt for that fire showed the run completed cleanly (error:null, 915k tokens,
27.6 min). Every downstream monitor then misread the run as a failure and the self-delete went
unnoticed for hours.

The fix: before discarding, re-check the durable receipt. A gone job record + a completed
usage_audit entry for that execution records the run as completed with a distinct
``self_removed`` delivery outcome — never 'failed'. Fail-closed variants (no receipt, errored
receipt, receipt from a different execution) must keep the discard.

These drive the real store (jobs.json + usage_audit.jsonl + executions.db under a temp
HERMES_HOME) so the assertion is the actual on-disk ledger row the monitors read.
"""

import pytest


@pytest.fixture
def temp_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME so jobs.json/usage_audit/executions don't touch the real store."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture(autouse=True)
def _clean_running_state():
    import cron.scheduler as sched

    sched._running_job_ids.clear()
    sched._running_fire_owners.clear()
    sched._interrupted_job_ids.clear()
    yield
    sched._running_job_ids.clear()
    sched._running_fire_owners.clear()
    sched._interrupted_job_ids.clear()


def _claimed_job():
    """A real recurring job record holding a live fire claim (as a firing tick has)."""
    from cron.jobs import claim_job_for_fire, create_job, get_job

    job = create_job(prompt="x", schedule="every 5m", name="selfdel")
    assert claim_job_for_fire(job["id"]) is True
    job = get_job(job["id"])
    assert isinstance(job.get("fire_claim"), dict) and job["fire_claim"].get("by")
    return job


def _remove_record_out_of_band(job_id: str) -> None:
    """Simulate the terminal-CLI subprocess removal: rewrite the store directly so the
    in-process ``self_removal_delivery`` ContextVar marker stays unarmed."""
    from cron.jobs import load_jobs, save_jobs

    save_jobs([j for j in load_jobs() if j["id"] != job_id], removed_ids={job_id})
    assert all(j["id"] != job_id for j in load_jobs())


def _write_receipt(sched, job_id: str, execution_id: str, *, error=None) -> None:
    """The usage_audit line ``run_job`` leaves when the agent run finishes."""
    sched._write_usage_audit({
        "ts": sched._utcnow_iso_ms(),
        "job_id": job_id,
        "fire_id": "f-selfdel",
        "execution_id": execution_id,
        "total_tokens": 915_000,
        "duration_ms": 1_656_000,
        "deliver_target": "origin",
        "error": error,
    })


def _drive(temp_home, monkeypatch, *, receipt):
    """One fire whose run deletes its own job record mid-run, then completes.

    ``receipt``: 'ok' writes a clean completed receipt, 'error' an errored one, 'none' nothing.
    """
    import cron.scheduler as sched

    job = _claimed_job()

    def fake_run_job(job, **kwargs):
        _remove_record_out_of_band(job["id"])
        if receipt == "ok":
            _write_receipt(sched, job["id"], kwargs["execution_id"])
        elif receipt == "error":
            _write_receipt(sched, job["id"], kwargs["execution_id"], error="boom")
        elif receipt == "other-execution":
            _write_receipt(sched, job["id"], "execution-of-a-different-fire")
        return True, "output text", "the report", None

    monkeypatch.setattr(sched, "run_job", fake_run_job)
    monkeypatch.setattr(sched, "_deliver_result", lambda *a, **k: None)
    return sched, job


def test_self_removed_job_with_completed_receipt_is_not_recorded_failed(temp_home, monkeypatch):
    """The sweep 119-120 path: job self-removed out-of-band + clean receipt → the ledger row is
    NOT status='failed'; it records the completed run with the distinct self_removed outcome."""
    from cron.executions import get_execution

    sched, job = _drive(temp_home, monkeypatch, receipt="ok")

    assert sched.run_one_job(job) is True

    record = get_execution(job["execution_id"])
    assert record["status"] != "failed", (
        "a delivered run whose job self-deleted must not be recorded as failed: "
        f"{record['error']!r}")
    assert record["status"] == "completed"
    assert record["delivery_outcome"] == "self_removed"
    assert record["error"] is None


def test_missing_receipt_stays_fail_closed_discard(temp_home, monkeypatch):
    """No usage_audit receipt for the fire → the stale-result discard is unchanged."""
    from cron.executions import get_execution

    sched, job = _drive(temp_home, monkeypatch, receipt="none")

    assert sched.run_one_job(job) is True

    record = get_execution(job["execution_id"])
    assert record["status"] == "failed"
    assert "discarded" in record["error"]


def test_errored_receipt_stays_fail_closed_discard(temp_home, monkeypatch):
    """The receipt shows the run itself errored → still a genuine failure."""
    from cron.executions import get_execution

    sched, job = _drive(temp_home, monkeypatch, receipt="error")

    assert sched.run_one_job(job) is True

    record = get_execution(job["execution_id"])
    assert record["status"] == "failed"
    assert "discarded" in record["error"]


def test_receipt_from_a_different_execution_does_not_rescue(temp_home, monkeypatch):
    """Strict execution correlation: an older fire's clean receipt is not this run's proof."""
    from cron.executions import get_execution

    sched, job = _drive(temp_home, monkeypatch, receipt="other-execution")

    assert sched.run_one_job(job) is True

    record = get_execution(job["execution_id"])
    assert record["status"] == "failed"
    assert "discarded" in record["error"]


def test_fire_audit_receipt_carries_execution_id(temp_home):
    """The receipt schema gained execution_id so the ledger row and the audit line correlate."""
    import json

    from cron.scheduler import _FireAudit

    audit = _FireAudit({"id": "j1", "deliver": "origin"}, "j1", "model-x", "exec-42")
    audit.write({"total_tokens": 7}, None)
    path = _usage_audit_path_for_test()
    entry = json.loads(path.read_text(encoding="utf-8").splitlines()[-1])
    assert entry["execution_id"] == "exec-42"
    assert entry["job_id"] == "j1"
    assert entry["error"] is None


def _usage_audit_path_for_test():
    from cron.scheduler import _usage_audit_path

    return _usage_audit_path()
