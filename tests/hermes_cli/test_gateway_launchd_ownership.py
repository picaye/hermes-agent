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


def test_off_darwin_claim_is_not_disproven(monkeypatch):
    """Custom systemd units and non-macOS supervisors keep today's hand-back behaviour.

    Pin the platform (t_8eb8d661): every other test in this file pins ``darwin`` via the
    ``darwin`` fixture; without a pin this one runs the REAL launchd probe on a macOS host,
    where fake PID 4242 is owned by no job and the off-darwin short-circuit is not exercised.
    """
    monkeypatch.setattr(gw.sys, "platform", "linux")
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
#
# Precedence ruling (t_8eb8d661, host gate on t_4d8f6e29): an EXPLICIT --external-supervisor
# promise (argv flag, which the gateway also stamps as HERMES_GATEWAY_EXTERNAL_SUPERVISOR in its
# own env) outranks the launchd-ownership probe on EVERY platform at the updater sites — the same
# order gateway.restart.service_restart_route_trusted() uses and test_darwin_explicit_optin_env_not_disproven
# pins at the probe seam. The probe decides only AMBIENT launchd claims (self-declared
# supervisor=="launchd", bare XPC_SERVICE_NAME) — the marker shape that stranded the fleet in
# t_946dd633.


def test_darwin_updater_route_explicit_flag_outranks_ownership_probe(monkeypatch):
    """Updater seam mirror of ``test_darwin_explicit_optin_env_not_disproven``: with the explicit
    flag on the gateway's argv the route must return the hand-back WITHOUT consulting the probe."""
    monkeypatch.setattr(gw.sys, "platform", "darwin")
    monkeypatch.setattr(gw, "get_launchd_label", lambda: LABEL)
    monkeypatch.setattr(gw, "_capture_gateway_argv",
                        lambda pid: ["python", "-m", "hermes_cli.main", "gateway", "run", "--external-supervisor"])

    def _never(pid):
        raise AssertionError("the launchd-ownership probe must not gate an explicit opt-in")

    monkeypatch.setattr(gw, "_launchd_owns_gateway_pid", _never)
    monkeypatch.setattr(gw, "launch_detached_profile_gateway_restart",
                        lambda *_a: pytest.fail("detached watcher must not be launched"))

    assert gw._prepare_profile_gateway_update_restart("work", GATEWAY_PID) == "external-supervisor"


def test_update_restart_arms_own_watcher_when_no_explicit_flag(monkeypatch):
    """Without the explicit promise the updater still arms its own watcher — and a gateway whose
    ambient launchd claim is disproven takes the detached self-relaunch at its own seam
    (pause-for-update reports restart_route='detached'), which is what the 13:04-15:28 outage
    lacked (t_946dd633)."""
    monkeypatch.setattr(gw.sys, "platform", "darwin")
    monkeypatch.setattr(gw, "get_launchd_label", lambda: LABEL)
    monkeypatch.setattr(gw, "_capture_gateway_argv",
                        lambda pid: ["python", "-m", "hermes_cli.main", "gateway", "run", "--replace"])
    armed = []
    monkeypatch.setattr(gw, "launch_detached_profile_gateway_restart",
                        lambda profile, pid: armed.append((profile, pid)) or True)

    mode = gw._prepare_profile_gateway_update_restart("default", GATEWAY_PID)

    assert mode == "detached"
    assert armed == [("default", GATEWAY_PID)]


def test_update_restart_still_hands_back_to_a_real_launchd_job(monkeypatch):
    """No behavior change for genuine launchd-managed instances — and the flag short-circuits
    before the probe on every platform."""
    monkeypatch.setattr(gw, "_capture_gateway_argv",
                        lambda pid: ["python", "-m", "hermes_cli.main", "gateway", "run", "--external-supervisor"])

    def _never(pid):
        raise AssertionError("launchctl must not be consulted for an explicit opt-in")

    monkeypatch.setattr(gw, "_launchd_owns_gateway_pid", _never)

    assert gw._prepare_profile_gateway_update_restart("default", GATEWAY_PID) == "external-supervisor"


def test_posix_pause_flag_beats_probe_on_darwin(darwin, monkeypatch):
    """``hermes update``'s pause route treats the explicit flag as the promise, never probing
    launchctl — precedence identical to the restart route on every platform."""
    from hermes_cli import update_cmd_posix_pause as posix_pause

    monkeypatch.setattr(gw, "_capture_gateway_argv",
                        lambda pid: ["python", "-m", "hermes_cli.main", "gateway", "run", "--external-supervisor"])

    def _never(pid):
        raise AssertionError("launchctl must not be consulted for an explicit opt-in")

    monkeypatch.setattr(gw, "_launchd_owns_gateway_pid", _never)
    assert posix_pause._supervisor_markers(GATEWAY_PID) == "--external-supervisor"


def test_posix_pause_ambient_launchd_claim_is_disproven(darwin, monkeypatch):
    """The incident shape at the updater pause route: launchd identity WITHOUT the explicit flag
    must not be trusted when no job runs the process — otherwise the updater pauses and the
    'respawn on old code' never happens."""
    from hermes_cli import update_cmd_posix_pause as posix_pause

    monkeypatch.setattr(gw, "_capture_gateway_argv", lambda pid: ["python", "-m", "hermes_cli.main", "gateway", "run"])
    monkeypatch.setattr(posix_pause, "_respawning_service", lambda pid: None)
    _fake_psutil(monkeypatch, {"XPC_SERVICE_NAME": LABEL})
    monkeypatch.setattr(gr, "launchd_running_job_pid", lambda label: None)
    assert posix_pause._supervisor_markers(GATEWAY_PID) is None


def test_gateway_declares_external_supervisor_false_when_job_runs_nothing(
    monkeypatch, tmp_path
):
    """``hermes gateway restart`` must not hand an AMBIENT launchd marker gateway to ``SIGUSR1 +
    wait for launchd`` — that path has no restart observer, so a disproven claim now falls through
    to the manual stop + foreground-start fallback, which recovers the gateway by itself."""
    monkeypatch.setattr(gw.sys, "platform", "darwin")
    monkeypatch.setattr("gateway.control_socket.identify_gateway",
                        lambda home, **k: {"pid": GATEWAY_PID, "launch_supervisor": "launchd"})
    monkeypatch.setattr(gw, "_capture_gateway_argv", lambda pid: None)
    monkeypatch.setattr(gw, "_launchd_owns_gateway_pid", lambda pid: False)
    assert supervised.gateway_declares_external_supervisor(GATEWAY_PID, home=tmp_path) is False


def test_gateway_declares_external_supervisor_true_for_owned_process(monkeypatch, tmp_path):
    monkeypatch.setattr(gw.sys, "platform", "darwin")
    monkeypatch.setattr("gateway.control_socket.identify_gateway",
                        lambda home, **k: {"pid": GATEWAY_PID, "launch_supervisor": "launchd"})
    monkeypatch.setattr(gw, "_capture_gateway_argv", lambda pid: None)
    monkeypatch.setattr(gw, "_launchd_owns_gateway_pid", lambda pid: True)
    assert supervised.gateway_declares_external_supervisor(GATEWAY_PID, home=tmp_path) is True


def test_gateway_declares_external_supervisor_explicit_flag_beats_probe(monkeypatch, tmp_path):
    """Precedence at the ``gateway restart`` site too: an explicit ``--external-supervisor`` argv
    promise is honored without consulting launchctl, on every platform."""
    monkeypatch.setattr(gw.sys, "platform", "darwin")
    monkeypatch.setattr("gateway.control_socket.identify_gateway", lambda home, **k: None)
    monkeypatch.setattr(gw, "_capture_gateway_argv",
                        lambda pid: ["python", "-m", "hermes_cli.main", "gateway", "run", "--external-supervisor"])

    def _never(pid):
        raise AssertionError("launchctl must not be consulted for an explicit opt-in")

    monkeypatch.setattr(gw, "_launchd_owns_gateway_pid", _never)
    assert supervised.gateway_declares_external_supervisor(GATEWAY_PID, home=tmp_path) is True
