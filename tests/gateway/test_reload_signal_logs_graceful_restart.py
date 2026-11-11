"""SIGUSR1 (systemd ``ExecReload`` / ``hermes gateway restart``) is a graceful restart, and the
gateway says so in its log when the signal lands (#117267)."""

from __future__ import annotations

import logging
import signal

import pytest

import gateway.run as gateway_run
from gateway.config import GatewayConfig


class _AbortedStartupRunner:
    """Aborts before running mode so start_gateway() returns right after installing signal handlers."""

    def __init__(self, config):
        # The unified bootstrap builds the session authority against the runner's real store
        # and stops it on every exit path (same shape as test_startup_restart_race.py).
        from gateway.session import SessionStore
        from hermes_state import SessionDB
        from hermes_constants import get_hermes_home
        self.session_store = SessionStore(get_hermes_home() / 'sessions', config)
        self._session_db = SessionDB(get_hermes_home() / 'state.db')
        self.session_store._db = self._session_db
        self._draining = False
        self.config = config
        self.adapters = {}
        self._running = False
        self._restart_requested = False
        self._restart_via_service = False
        self.should_exit_cleanly = True
        self.should_exit_with_failure = False
        self.exit_reason = None
        self.exit_code = None
        self.restart_calls = []

    async def start(self):
        return False

    async def wait_for_shutdown(self):
        return None

    async def stop(self):
        self.session_store.close_all_db_handles()
        self._session_db.close()

    def request_restart(self, *, detached=False, via_service=False):
        self.restart_calls.append({"detached": detached, "via_service": via_service})
        return True


@pytest.mark.asyncio
async def test_sigusr1_handler_installed_by_start_gateway_logs_graceful_restart(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    # Hermetic signal-handler route pin (t_8eb8d661): the SIGUSR1 handler now consults
    # gateway.restart.service_restart_route_trusted(), which reads this process's live environment.
    # On hosts whose session carries launchd-shaped markers (macOS Aqua sessions stamp
    # XPC_SERVICE_NAME on everything) the seam disproves ownership for a test child and flips the
    # call to the detached fallback. Clear the ambient markers and pin an unconditionally trusted
    # per-launch one so the assertion stands on every platform.
    for var in ("XPC_SERVICE_NAME", "HERMES_LAUNCHD_LABEL", "HERMES_S6_SUPERVISED_CHILD",
                "HERMES_GATEWAY_EXTERNAL_SUPERVISOR", "HERMES_SUPERVISED_CHILD"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("INVOCATION_ID", "sigusr1-graceful-restart-test")
    for target, value in (
        ("gateway.status.get_running_pid", lambda: None),
        ("gateway.status.acquire_gateway_runtime_lock", lambda: True),
        ("gateway.status.write_pid_file", lambda: None),
        ("gateway.status.remove_pid_file", lambda: None),
        ("gateway.status.release_gateway_runtime_lock", lambda: None),
        ("tools.skills_sync.sync_skills", lambda quiet=True: None),
        ("hermes_logging.setup_logging", lambda hermes_home, mode: None),
        ("tools.mcp_tool_lifecycle.shutdown_mcp_servers", lambda: None),
    ):
        monkeypatch.setattr(target, value)
    runners = []

    def make_runner(config):
        runners.append(_AbortedStartupRunner(config))
        return runners[-1]

    monkeypatch.setattr(gateway_run, "GatewayRunner", make_runner)

    import asyncio
    loop = asyncio.get_running_loop()
    installed = {}
    monkeypatch.setattr(loop, "add_signal_handler", lambda sig, handler, *args: installed.update({sig: (handler, args)}))

    await gateway_run.start_gateway(config=GatewayConfig(), replace=False, verbosity=None)

    handler, args = installed[signal.SIGUSR1]
    with caplog.at_level(logging.INFO, logger="gateway.run"):
        handler(*args)

    assert runners[0].restart_calls == [{"detached": False, "via_service": True}]
