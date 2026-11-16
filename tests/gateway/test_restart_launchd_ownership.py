"""t_4d8f6e29 — the exit-75 service hand-back must verify launchd OWNERSHIP, not marker existence.

Incident (root-caused in t_946dd633, 2026-11-04 13:04:28): a boot-started gateway honored a
restart request from an in-flight ``hermes update``, drained cleanly and raised SystemExit(75)
because a launchd-shaped marker answered TRUE — but the LaunchAgent had never spawned that
process (launchd logged "pending spawn, domain in on-demand-only mode"; the job's first run came
only at 15:30 after a manual Aqua-session bootstrap). Nobody relaunched the gateway; the fleet
was dead ~2h25m. KeepAlive would have respawned a GENUINE launchd child correctly — the defect is
trust-vs-ownership only.

The rule under test: the PID ``launchctl`` reports as the job's must be the process itself or one
of its ancestors before exit-75 may be chosen; otherwise take the detached self-relaunch path.
"""
from unittest.mock import MagicMock

import pytest

import gateway.restart as gr

LABEL = "ai.hermes.gateway"


class _NeverCalled:
    def __call__(self, *a, **k):
        raise AssertionError("launchctl/ancestry must not be consulted on this route")


def _grandchild_env(label=LABEL):
    """launchd stamps XPC only on the job process (the plist's stderr-timestamp wrapper); the
    gateway grandchild below it sees XPC ``0`` plus the wrapper's re-exported label."""
    return {"XPC_SERVICE_NAME": "0", gr.LAUNCHD_LABEL_ENV: label}


# ---------------------------------------------------------------- parse_launchd_job_pid

def test_parse_launchd_job_pid_accepts_both_query_shapes():
    assert gr.parse_launchd_job_pid('  "PID" = 12378;') == 12378
    assert gr.parse_launchd_job_pid("PID = 42;") == 42
    assert gr.parse_launchd_job_pid("\t\tpid = 77") == 77


def test_parse_launchd_job_pid_none_when_job_runs_no_process():
    assert gr.parse_launchd_job_pid('"Label" = ai.hermes.gateway\n"PID" = -1;') is None
    assert gr.parse_launchd_job_pid('"Label" = ai.hermes.gateway') is None  # registered, never spawned
    assert gr.parse_launchd_job_pid("") is None


def test_launchd_running_job_pid_fail_closed_on_query_error():
    def _boom(*a, **k):
        raise FileNotFoundError("launchctl")

    assert gr.launchd_running_job_pid(LABEL, run=_boom) is None


# --------------------------------------------------------- route decision matrix (the seam)

def test_systemd_invocation_id_trusted_without_launchctl():
    """No behavior change for genuine systemd instances: per-launch marker, launchctl not consulted."""
    trusted, reason = gr.service_restart_route_trusted(
        {"INVOCATION_ID": "abc"}, pid=100, job_pid_fn=_NeverCalled())
    assert trusted, reason


def test_s6_sentinel_trusted_without_launchctl():
    trusted, reason = gr.service_restart_route_trusted(
        {"HERMES_S6_SUPERVISED_CHILD": "1"}, pid=100, job_pid_fn=_NeverCalled())
    assert trusted, reason


def test_explicit_external_supervisor_optin_env_trusted():
    trusted, reason = gr.service_restart_route_trusted(
        {gr.EXTERNAL_GATEWAY_SUPERVISOR_ENV: "true"}, pid=100, job_pid_fn=_NeverCalled())
    assert trusted, reason


def test_launchd_direct_child_matching_job_pid_trusted():
    trusted, reason = gr.service_restart_route_trusted(
        {"XPC_SERVICE_NAME": LABEL}, pid=100, job_pid_fn=lambda label: 100, ancestors_fn=_NeverCalled())
    assert trusted, reason


def test_launchd_grandchild_below_wrapper_trusted():
    """No behavior change for genuine launchd-managed instances: the wrapper (job pid) is an
    ancestor of the gateway grandchild."""
    trusted, reason = gr.service_restart_route_trusted(
        _grandchild_env(), pid=100, job_pid_fn=lambda label: 90, ancestors_fn=lambda pid: [90, 1])
    assert trusted, reason


def test_registered_but_never_spawned_job_not_trusted_incident_shape():
    """The 13:04:28 shape: the gateway carried launchd identity, the job ran no process."""
    trusted, reason = gr.service_restart_route_trusted(
        _grandchild_env(), pid=100, job_pid_fn=lambda label: None, ancestors_fn=_NeverCalled())
    assert not trusted
    assert "runs no process" in reason


def test_job_running_a_different_process_not_trusted():
    trusted, reason = gr.service_restart_route_trusted(
        _grandchild_env(), pid=100, job_pid_fn=lambda label: 90, ancestors_fn=lambda pid: [5, 1])
    assert not trusted
    assert "did not spawn" in reason


@pytest.mark.parametrize("xpc", ["application.com.apple.Terminal.1234", "0x1000a1b2c3.com.apple.x"])
def test_bare_aqua_session_xpc_is_not_supervision(xpc, monkeypatch):
    """A desktop-session launch inherits an app-coalition XPC name: existence of SOME XPC value
    must never select the exit-75 hand-back."""
    monkeypatch.setattr(gr.sys, "platform", "darwin")
    trusted, reason = gr.service_restart_route_trusted(
        {"XPC_SERVICE_NAME": xpc}, pid=100, job_pid_fn=_NeverCalled())
    assert not trusted
    assert "ai.hermes" in reason


def test_no_markers_not_trusted():
    trusted, reason = gr.service_restart_route_trusted({}, pid=100, job_pid_fn=_NeverCalled())
    assert not trusted
    assert "no service-manager marker" in reason


# ------------------------------------------------------------- SIGUSR1 handler wiring

def _fire_sigusr1(monkeypatch, runner, *, route, marker_lie_env):
    import gateway.run as gateway_run

    for key, value in marker_lie_env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(gr, "service_restart_route_trusted", lambda **kw: route)
    handler = gateway_run._start_gateway_make_restart_signal_handler(runner)
    handler()


def test_sigusr1_untrusted_falls_back_to_detached_self_relaunch(monkeypatch, caplog):
    """A gateway whose markers lie (job never spawned it) must NOT go exit-75 dark: SIGUSR1 now
    routes it to the detached self-relaunch helper, loudly."""
    runner = MagicMock()
    caplog.set_level("ERROR", logger="gateway.run")
    _fire_sigusr1(
        monkeypatch, runner,
        route=(False, f"launchd job {LABEL!r} runs no process"),
        marker_lie_env={"XPC_SERVICE_NAME": LABEL},
    )
    runner.request_restart.assert_called_once_with(detached=True, via_service=False)
    assert "refusing the exit-75 service hand-back" in caplog.text


def test_sigusr1_trusted_keeps_service_handback(monkeypatch):
    runner = MagicMock()
    _fire_sigusr1(monkeypatch, runner, route=(True, "systemd INVOCATION_ID ..."),
                  marker_lie_env={"INVOCATION_ID": "abc"})
    runner.request_restart.assert_called_once_with(detached=False, via_service=True)
