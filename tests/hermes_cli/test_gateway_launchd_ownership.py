"""t_4d8f6e29 — updater-side trust-vs-ownership: ``--external-supervisor`` / launchd markers on a
live gateway must be confirmed by the job actually running that process (or its wrapper ancestor)
before ``hermes update`` hands it back with exit 75.

Incident recap (root cause t_946dd633): the updater saw ``--external-supervisor`` on the argv of a
boot-started gateway, chose the "hand back to launchd" route, the gateway drained + exited 75, and
launchd — which had NEVER spawned that process ("pending spawn, domain in on-demand-only mode",
runs=0 all day) — relaunched nothing. The fleet stayed dead until a manual Aqua-session bootstrap.
"""
import pytest

import gateway.restart as gr
from hermes_cli import gateway as gw
from hermes_cli import gateway_supervised_restart as supervised

LABEL = "ai.hermes.gateway"
GATEWAY_PID = 4321
WRAPPER_PID = 4300


def _fake_psutil(monkeypatch, env):
    class _P:
        def __init__(self, pid):
            pass

        def environ(self):
            return dict(env)

    monkeypatch.setattr("psutil.Process", _P)


@pytest.fixture
def darwin(monkeypatch):
    monkeypatch.setattr(gw.sys, "platform", "darwin")
    monkeypatch.setattr(gw, "get_launchd_label", lambda: LABEL)


def test_off_darwin_claim_is_not_disproven():
    """Custom systemd units and non-macOS supervisors keep today's hand-back behaviour."""
    assert gw._launchd_owns_gateway_pid(GATEWAY_PID) is True


def test_darwin_marker_with_no_running_job_is_disproven(darwin, monkeypatch):
    """The incident shape: launchd identity markers everywhere, job supervises no process."""
    _fake_psutil(monkeypatch, {"XPC_SERVICE_NAME": "0", gr.LAUNCHD_LABEL_ENV: LABEL})
    monkeypatch.setattr(gr, "launchd_running_job_pid", lambda label: None)
    assert gw._launchd_owns_gateway_pid(GATEWAY_PID) is False


def test_darwin_exact_job_pid_match_is_owned(darwin, monkeypatch):
    _fake_psutil(monkeypatch, {"XPC_SERVICE_NAME": LABEL})
    monkeypatch.setattr(gr, "launchd_running_job_pid", lambda label: GATEWAY_PID)

    class _Never:
        def __call__(self, pid):
            raise AssertionError("ancestry walk must not run on a direct match")

    monkeypatch.setattr(gr, "process_ancestor_pids", _Never())
    assert gw._launchd_owns_gateway_pid(GATEWAY_PID) is True


def test_darwin_wrapper_ancestor_is_owned(darwin, monkeypatch):
    """A genuine launchd-managed grandchild: the job runs the plist wrapper, above us."""
    _fake_psutil(monkeypatch, {"XPC_SERVICE_NAME": "0", gr.LAUNCHD_LABEL_ENV: LABEL})
    monkeypatch.setattr(gr, "launchd_running_job_pid", lambda label: WRAPPER_PID)
    monkeypatch.setattr(gr, "process_ancestor_pids", lambda pid: [WRAPPER_PID, 1])
    assert gw._launchd_owns_gateway_pid(GATEWAY_PID) is True


def test_darwin_unrelated_job_pid_is_not_ownership(darwin, monkeypatch):
    _fake_psutil(monkeypatch, {"XPC_SERVICE_NAME": "0", gr.LAUNCHD_LABEL_ENV: LABEL})
    monkeypatch.setattr(gr, "launchd_running_job_pid", lambda label: 999)
    monkeypatch.setattr(gr, "process_ancestor_pids", lambda pid: [WRAPPER_PID, 1])
    assert gw._launchd_owns_gateway_pid(GATEWAY_PID) is False


def test_darwin_explicit_optin_env_not_disproven(darwin, monkeypatch):
    """The operator-set ``HERMES_GATEWAY_EXTERNAL_SUPERVISOR`` promise outranks launchctl."""
    _fake_psutil(monkeypatch, {gr.EXTERNAL_GATEWAY_SUPERVISOR_ENV: "1"})

    def _never(label):
        raise AssertionError("launchctl must not be consulted for an explicit opt-in")

    monkeypatch.setattr(gr, "launchd_running_job_pid", _never)
    assert gw._launchd_owns_gateway_pid(GATEWAY_PID) is True


# ------------------------------------------------------------------ route integration

def test_update_restart_arms_own_watcher_when_launchd_does_not_own_the_process(
    monkeypatch, tmp_path
):
    """``_prepare_profile_gateway_update_restart`` must stop returning "external-supervisor"
    (which leaves the fleet to launchd, which never ran it) and arm the updater's detached
    watcher instead — the recovery the 13:04-15:28 outage lacked."""
    monkeypatch.setattr(gw.sys, "platform", "darwin")
    monkeypatch.setattr(gw, "get_launchd_label", lambda: LABEL)
    monkeypatch.setattr(gw, "_capture_gateway_argv",
                        lambda pid: ["python", "-m", "hermes_cli.main", "gateway", "run", "--external-supervisor"])
    monkeypatch.setattr(gw, "_launchd_owns_gateway_pid", lambda pid: False)
    armed = []
    monkeypatch.setattr(gw, "launch_detached_profile_gateway_restart",
                        lambda profile, pid: armed.append((profile, pid)) or True)

    mode = gw._prepare_profile_gateway_update_restart("default", GATEWAY_PID)

    assert mode == "detached"
    assert armed == [("default", GATEWAY_PID)]


def test_update_restart_still_hands_back_to_a_real_launchd_job(monkeypatch):
    """No behavior change for genuine launchd-managed instances."""
    monkeypatch.setattr(gw, "_capture_gateway_argv",
                        lambda pid: ["python", "-m", "hermes_cli.main", "gateway", "run", "--external-supervisor"])
    monkeypatch.setattr(gw, "_launchd_owns_gateway_pid", lambda pid: True)

    assert gw._prepare_profile_gateway_update_restart("default", GATEWAY_PID) == "external-supervisor"


def test_gateway_declares_external_supervisor_false_when_job_runs_nothing(
    monkeypatch, tmp_path
):
    """``hermes gateway restart`` must not hand a marker-only gateway to ``SIGUSR1 + wait for
    launchd`` — that path has no restart observer, so a disproven claim now falls through to the
    manual stop + foreground-start fallback, which recovers the gateway by itself."""
    monkeypatch.setattr(gw.sys, "platform", "darwin")
    monkeypatch.setattr("gateway.control_socket.identify_gateway", lambda home, **k: None)
    monkeypatch.setattr(gw, "_capture_gateway_argv",
                        lambda pid: ["python", "-m", "hermes_cli.main", "gateway", "run", "--external-supervisor"])
    monkeypatch.setattr(gw, "_launchd_owns_gateway_pid", lambda pid: False)
    assert supervised.gateway_declares_external_supervisor(GATEWAY_PID, home=tmp_path) is False


def test_gateway_declares_external_supervisor_true_for_owned_process(monkeypatch, tmp_path):
    monkeypatch.setattr(gw.sys, "platform", "darwin")
    monkeypatch.setattr("gateway.control_socket.identify_gateway", lambda home, **k: None)
    monkeypatch.setattr(gw, "_capture_gateway_argv",
                        lambda pid: ["python", "-m", "hermes_cli.main", "gateway", "run", "--external-supervisor"])
    monkeypatch.setattr(gw, "_launchd_owns_gateway_pid", lambda pid: True)
    assert supervised.gateway_declares_external_supervisor(GATEWAY_PID, home=tmp_path) is True
