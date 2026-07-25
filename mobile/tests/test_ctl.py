"""Tests for trustmux._ctl, _enable, _disable."""

import json
import os
import shutil
import signal
import socket
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, call, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import trustmux._ctl as ctl
import trustmux._enable as enable
import trustmux._disable as disable

_no_daemon = None
_tmp_root = None
_tmp_env = None


def setUpModule():
    # Root every base directory in a temp tree so the suite can never read or
    # write the developer's real trustmux state.
    global _no_daemon, _tmp_root, _tmp_env
    _tmp_root = tempfile.TemporaryDirectory()
    root = Path(_tmp_root.name)
    _tmp_env = patch.dict(os.environ, {
        'TRUSTMUX_CONFIG_DIR': str(root / 'config'),
        'TRUSTMUX_STATE_DIR':  str(root / 'state'),
    })
    _tmp_env.start()
    # resolve_port() asks the running daemon which port it is on; without this
    # the suite would pick up a real trustmux on the developer's machine.
    _no_daemon = patch('trustmux._ctl.daemon_info', return_value=None)
    _no_daemon.start()


def tearDownModule():
    _no_daemon.stop()
    _tmp_env.stop()
    _tmp_root.cleanup()


def write_pid(inst, pid):
    """Give inst a pid file containing pid."""
    inst.ensure_dirs()
    inst.pid_file.write_text(str(pid))
    return inst.pid_file


# ---------------------------------------------------------------------------
# _pid()
# ---------------------------------------------------------------------------

class TestPid(unittest.TestCase):
    """lsof's answer (found or not-found) is trusted outright and is final --
    it directly observes what owns the requested port. The only fallback is
    asking the running daemon what port *it* is on, which is used solely
    when lsof itself cannot run at all, and only trusted when the daemon's
    reported port matches the one actually being asked about.

    There must be no fallback that can return a pid without confirming it
    owns the *requested* port: that is what let a stale/unrelated PIDFILE
    entry cause `_pid(port)` to return a different daemon's pid for a port
    nothing was listening on, and in turn let `trustmux stop --port <wrong>`
    kill the wrong daemon (see TestPidCrossPortRegression below)."""

    def setUp(self):
        self.inst = ctl.Instance('pidtest')
        self.inst.ensure_dirs()
        self.addCleanup(shutil.rmtree, self.inst.state, True)

    def test_lsof_returns_pid(self):
        with patch('trustmux._ctl.subprocess.check_output', return_value='1234\n'):
            self.assertEqual(ctl._pid(3389, self.inst), 1234)

    def test_lsof_first_pid_when_this_instance_has_no_claim(self):
        with patch('trustmux._ctl.subprocess.check_output', return_value='1234\n5678\n'):
            self.assertEqual(ctl._pid(3389, self.inst), 1234)

    def test_lsof_empty_output_is_definitive_none(self):
        # lsof ran fine, found nothing: no fallback, no daemon_info() call.
        with patch('trustmux._ctl.subprocess.check_output', return_value=''):
            with patch('trustmux._ctl.daemon_info') as mock_info:
                self.assertIsNone(ctl._pid(3389, self.inst))
        mock_info.assert_not_called()

    def test_lsof_called_process_error_is_definitive_none(self):
        # The real, common case: lsof -ti:<port> exits non-zero when nothing
        # matches. Must NOT fall through to a port-blind fallback.
        import subprocess
        with patch('trustmux._ctl.subprocess.check_output',
                   side_effect=subprocess.CalledProcessError(1, 'lsof')):
            with patch('trustmux._ctl.daemon_info') as mock_info:
                self.assertIsNone(ctl._pid(3389, self.inst))
        mock_info.assert_not_called()

    def test_a_live_pidfile_cannot_conjure_a_pid_for_an_idle_port(self):
        # Same invariant as above, with this instance's pid file naming a
        # genuinely live process: still None, because nothing holds the port.
        import subprocess
        write_pid(self.inst, 9999)
        with patch('trustmux._ctl.subprocess.check_output',
                   side_effect=subprocess.CalledProcessError(1, 'lsof')):
            with patch('trustmux._ctl.os.kill', return_value=None):
                self.assertIsNone(ctl._pid(3389, self.inst))

    def test_lsof_missing_falls_back_to_daemon_info_matching_port(self):
        with patch('trustmux._ctl.subprocess.check_output',
                   side_effect=FileNotFoundError):
            with patch('trustmux._ctl.daemon_info',
                       return_value={'pid': 4321, 'port': 3389}):
                self.assertEqual(ctl._pid(3389, self.inst), 4321)

    def test_lsof_missing_and_daemon_info_reports_different_port(self):
        # The exact regression this class exists to prevent: a live daemon
        # on some OTHER port must never be returned for the requested one.
        with patch('trustmux._ctl.subprocess.check_output',
                   side_effect=FileNotFoundError):
            with patch('trustmux._ctl.daemon_info',
                       return_value={'pid': 4321, 'port': 7432}):
                self.assertIsNone(ctl._pid(3389, self.inst))

    def test_lsof_missing_and_no_daemon_answers(self):
        with patch('trustmux._ctl.subprocess.check_output',
                   side_effect=FileNotFoundError):
            with patch('trustmux._ctl.daemon_info', return_value=None):
                self.assertIsNone(ctl._pid(3389, self.inst))

    def test_lsof_missing_and_daemon_info_reports_junk_pid(self):
        with patch('trustmux._ctl.subprocess.check_output',
                   side_effect=FileNotFoundError):
            with patch('trustmux._ctl.daemon_info',
                       return_value={'pid': 'not-a-pid', 'port': 3389}):
                self.assertIsNone(ctl._pid(3389, self.inst))

    def test_lsof_full_listing_is_treated_as_unusable_not_as_a_pid(self):
        # An lsof that does not understand -ti:<port> prints a whole table
        # instead of failing; its columns must not be misread as pids.
        listing = '1084\t/usr/lib/systemd/systemd\t0\t/dev/null\n'
        with patch('trustmux._ctl.subprocess.check_output', return_value=listing):
            with patch('trustmux._ctl.daemon_info', return_value=None) as mock_info:
                self.assertIsNone(ctl._pid(3389, self.inst))
        mock_info.assert_called_once()


# ---------------------------------------------------------------------------
# Two instances, one port: attribution must not be a coin flip
# ---------------------------------------------------------------------------

class TestPidInstanceAttribution(unittest.TestCase):
    """Two daemons may share a port on different addresses (127.0.0.1:7432 and
    192.168.1.5:7432), so lsof reports both pids. The instance's own pid file
    says which of them is ours -- but only ever to *choose among* processes
    already known to hold the port, never to invent one."""

    def setUp(self):
        self.inst = ctl.Instance('attrib')
        self.inst.ensure_dirs()
        self.addCleanup(shutil.rmtree, self.inst.state, True)

    def test_prefers_own_pid_over_whichever_lsof_listed_first(self):
        write_pid(self.inst, 5678)
        with patch('trustmux._ctl.subprocess.check_output', return_value='1234\n5678\n'):
            with patch('trustmux._ctl.os.kill', return_value=None):
                self.assertEqual(ctl._pid(7432, self.inst), 5678)

    def test_another_instances_daemon_on_our_port_is_not_ours(self):
        # Our pid file names a live daemon, but it is not the one holding this
        # port -- so this instance is not running on it.
        write_pid(self.inst, 5678)
        with patch('trustmux._ctl.subprocess.check_output', return_value='1234\n'):
            with patch('trustmux._ctl.os.kill', return_value=None):
                self.assertIsNone(ctl._pid(7432, self.inst))

    def test_admin_socket_attributes_when_the_pidfile_is_gone(self):
        with patch('trustmux._ctl.subprocess.check_output', return_value='1234\n5678\n'):
            with patch('trustmux._ctl.daemon_info', return_value={'pid': 5678}):
                self.assertEqual(ctl._pid(7432, self.inst), 5678)


# ---------------------------------------------------------------------------
# Regression: _pid(port) must never return an unrelated daemon's pid for a
# port nothing is listening on (verified live: this previously let
# `trustmux stop --port <unrelated port>` kill an unrelated running daemon).
# ---------------------------------------------------------------------------

class TestPidCrossPortRegression(unittest.TestCase):

    def setUp(self):
        self.inst = ctl.Instance('crossport')
        self.inst.ensure_dirs()
        self.addCleanup(shutil.rmtree, self.inst.state, True)

    def test_stop_on_unrelated_port_does_not_kill_the_real_daemon(self):
        import subprocess as sp
        # Real daemon is on 3389 (the pid file reflects it, and it is
        # genuinely alive); lsof is queried for 4444, where nothing listens,
        # and genuinely finds nothing. Must never send SIGTERM to the pid the
        # pid file names -- only the harmless liveness probe (signal 0), used
        # to decide whether to also clean the pid file up.
        write_pid(self.inst, 3141)
        with patch('trustmux._ctl.subprocess.check_output',
                   side_effect=sp.CalledProcessError(1, 'lsof')):
            with patch('trustmux._ctl.daemon_info', return_value=None):
                with patch('trustmux._ctl.os.kill', return_value=None) as mock_kill:
                    with patch('builtins.print'):
                        result = ctl.cmd_stop(4444, self.inst)
        self.assertNotIn(call(3141, signal.SIGTERM), mock_kill.call_args_list)
        for c in mock_kill.call_args_list:
            self.assertEqual(c, call(3141, 0))   # liveness probes only
        self.assertEqual(result, 0)

    def test_live_pidfile_survives_a_stop_on_a_different_port(self):
        import subprocess as sp
        write_pid(self.inst, 3141)
        with patch('trustmux._ctl.subprocess.check_output',
                   side_effect=sp.CalledProcessError(1, 'lsof')):
            with patch('trustmux._ctl.daemon_info', return_value=None):
                with patch('trustmux._ctl.os.kill', return_value=None):
                    with patch('builtins.print'):
                        ctl.cmd_stop(4444, self.inst)
        self.assertTrue(self.inst.pid_file.exists())

    def test_dead_pidfile_is_cleaned_up(self):
        import subprocess as sp
        write_pid(self.inst, 3141)
        with patch('trustmux._ctl.subprocess.check_output',
                   side_effect=sp.CalledProcessError(1, 'lsof')):
            with patch('trustmux._ctl.daemon_info', return_value=None):
                with patch('trustmux._ctl.os.kill', side_effect=ProcessLookupError):
                    with patch('builtins.print'):
                        ctl.cmd_stop(4444, self.inst)
        self.assertFalse(self.inst.pid_file.exists())

    def test_start_on_unrelated_port_is_not_falsely_refused(self):
        import subprocess as sp
        with patch('trustmux._ctl.subprocess.check_output',
                   side_effect=sp.CalledProcessError(1, 'lsof')):
            with patch('trustmux._ctl.daemon_info', return_value=None):
                with patch('trustmux._ctl._launch', return_value=1234) as mock_launch:
                    with patch('builtins.print'):
                        result = ctl.cmd_start('start-local', 4444, self.inst)
        mock_launch.assert_called_once()
        self.assertEqual(result, 0)

# ---------------------------------------------------------------------------
# _ts_host()
# ---------------------------------------------------------------------------

class TestTsHost(unittest.TestCase):

    def _ts_json(self, dns='engawa.ts.net.'):
        return json.dumps({'Self': {'DNSName': dns}})

    def test_returns_name_without_trailing_dot(self):
        with patch('trustmux._ctl.subprocess.check_output',
                   return_value=self._ts_json()):
            self.assertEqual(ctl._ts_host(), 'engawa.ts.net')

    def test_empty_when_no_self_key(self):
        with patch('trustmux._ctl.subprocess.check_output',
                   return_value=json.dumps({})):
            self.assertEqual(ctl._ts_host(), '')

    def test_empty_on_subprocess_error(self):
        import subprocess
        with patch('trustmux._ctl.subprocess.check_output',
                   side_effect=subprocess.CalledProcessError(1, 'tailscale')):
            self.assertEqual(ctl._ts_host(), '')

    def test_empty_when_tailscale_not_found(self):
        with patch('trustmux._ctl.subprocess.check_output',
                   side_effect=FileNotFoundError):
            self.assertEqual(ctl._ts_host(), '')


# ---------------------------------------------------------------------------
# _check_tls()  — GH #113: missing cryptography in bundled Homebrew venv
# ---------------------------------------------------------------------------

class TestCheckTls(unittest.TestCase):

    @unittest.skipUnless(
        __import__('importlib.util', fromlist=['find_spec']).find_spec('cryptography') is not None,
        'cryptography not installed in this venv',
    )
    def test_returns_true_when_cryptography_present(self):
        self.assertTrue(ctl._check_tls())

    def test_returns_false_and_prints_sys_executable_when_import_fails(self):
        # Simulate a Homebrew bundled-venv install where cryptography is absent
        # (GH #113).  Shadow the module so the local `from cryptography...`
        # import inside _check_tls raises ImportError.
        import io
        buf = io.StringIO()
        with patch.dict(sys.modules, {
            'cryptography.hazmat.primitives.asymmetric': None,
        }):
            with patch('trustmux._ctl.sys.stderr', buf):
                result = ctl._check_tls()
        self.assertFalse(result)
        output = buf.getvalue()
        self.assertIn(sys.executable, output)
        self.assertIn("cryptography", output)
        # Must NOT suggest bare 'pip' — that targets the wrong interpreter
        # in Homebrew's bundled-venv installs (regression: GH #113).
        for line in output.splitlines():
            if "pip" in line and "cryptography" in line:
                self.assertNotRegex(line, r'^\s+pip ')


# ---------------------------------------------------------------------------
# _ensure_ts_serve()
# ---------------------------------------------------------------------------

class TestEnsureTsServe(unittest.TestCase):

    def test_already_configured(self):
        port_str = f':{ctl.DEFAULT_PORT}'
        with patch('trustmux._ctl.subprocess.check_output',
                   return_value=f'https/tcp/0:443 → {port_str}\n'):
            with patch('trustmux._ctl.subprocess.run') as mock_run:
                result = ctl._ensure_ts_serve()
        self.assertTrue(result)
        mock_run.assert_not_called()

    def test_configures_on_first_attempt(self):
        with patch('trustmux._ctl.subprocess.check_output',
                   return_value='no matching port\n'):
            with patch('trustmux._ctl.subprocess.run') as mock_run:
                mock_run.return_value = MagicMock(returncode=0)
                result = ctl._ensure_ts_serve()
        self.assertTrue(result)

    def test_prints_error_and_returns_false_when_serve_fails(self):
        import subprocess as sp
        with patch('trustmux._ctl.subprocess.check_output', return_value='nothing'):
            with patch('trustmux._ctl.subprocess.run',
                       side_effect=sp.CalledProcessError(1, 'tailscale')) as mock_run:
                result = ctl._ensure_ts_serve()
        self.assertFalse(result)
        # Must never auto-run sudo
        for call in mock_run.call_args_list:
            self.assertNotIn('sudo', call.args[0])

    def test_returns_false_when_all_attempts_fail(self):
        import subprocess as sp
        with patch('trustmux._ctl.subprocess.check_output', return_value='nothing'):
            with patch('trustmux._ctl.subprocess.run',
                       side_effect=sp.CalledProcessError(1, 'tailscale')):
                result = ctl._ensure_ts_serve()
        self.assertFalse(result)


# ---------------------------------------------------------------------------
# cmd_setup()
# ---------------------------------------------------------------------------

class TestCmdSetup(unittest.TestCase):

    def _patch_ok(self):
        return {
            'trustmux._ctl.subprocess.run': MagicMock(return_value=MagicMock(returncode=0)),
            'trustmux._ctl._ts_host': MagicMock(return_value='engawa.ts.net'),
            'trustmux._ctl._ensure_ts_serve': MagicMock(return_value=True),
        }

    def test_returns_1_when_package_not_importable(self):
        with patch.dict('sys.modules', {'trustmux._daemon': None}):
            with patch('builtins.__import__', side_effect=ImportError):
                # patch the import inside cmd_setup
                pass
        # Simpler: patch the import inside the function
        import builtins
        real_import = builtins.__import__
        def fake_import(name, *a, **kw):
            if name == 'trustmux._daemon':
                raise ImportError
            return real_import(name, *a, **kw)
        with patch('builtins.__import__', side_effect=fake_import):
            result = ctl.cmd_setup()
        self.assertEqual(result, 1)

    def test_returns_1_when_tailscale_missing(self):
        import subprocess as sp
        with patch('trustmux._ctl._ts_host', return_value='engawa.ts.net'):
            with patch('trustmux._ctl._ensure_ts_serve', return_value=True):
                with patch('trustmux._ctl.subprocess.run',
                           side_effect=FileNotFoundError):
                    result = ctl.cmd_setup()
        self.assertEqual(result, 1)

    def test_returns_1_when_tailscale_not_connected(self):
        with patch('trustmux._ctl.subprocess.run'):
            with patch('trustmux._ctl._ts_host', return_value=''):
                with patch('trustmux._ctl._ensure_ts_serve', return_value=True):
                    result = ctl.cmd_setup()
        self.assertEqual(result, 1)

    def test_returns_1_when_ts_serve_fails(self):
        with patch('trustmux._ctl.subprocess.run'):
            with patch('trustmux._ctl._ts_host', return_value='engawa.ts.net'):
                with patch('trustmux._ctl._ensure_ts_serve', return_value=False):
                    result = ctl.cmd_setup()
        self.assertEqual(result, 1)

    def test_returns_0_on_success(self):
        with patch('trustmux._ctl.subprocess.run'):
            with patch('trustmux._ctl._ts_host', return_value='engawa.ts.net'):
                with patch('trustmux._ctl._ensure_ts_serve', return_value=True):
                    result = ctl.cmd_setup()
        self.assertEqual(result, 0)

    def test_quiet_suppresses_next_steps(self):
        with patch('trustmux._ctl.subprocess.run'):
            with patch('trustmux._ctl._ts_host', return_value='engawa.ts.net'):
                with patch('trustmux._ctl._ensure_ts_serve', return_value=True):
                    with patch('builtins.print') as mock_print:
                        ctl.cmd_setup(quiet=True)
        printed = ' '.join(str(c) for c in mock_print.call_args_list)
        self.assertNotIn('Next steps', printed)


# ---------------------------------------------------------------------------
# cmd_start()
# ---------------------------------------------------------------------------

class TestCmdStart(unittest.TestCase):

    def test_returns_1_when_already_running(self):
        with patch('trustmux._ctl._pid', return_value=1234):
            self.assertEqual(ctl.cmd_start(), 1)

    def test_returns_1_when_tailscale_missing(self):
        with patch('trustmux._ctl._pid', return_value=None):
            with patch('trustmux._ctl._check_tmux', return_value=True):
                with patch('trustmux._ctl._check_tls', return_value=True):
                    with patch('trustmux._ctl.subprocess.run',
                               side_effect=FileNotFoundError):
                        self.assertEqual(ctl.cmd_start('serve'), 1)

    def test_returns_1_when_no_tailscale_host(self):
        with patch('trustmux._ctl._pid', return_value=None):
            with patch('trustmux._ctl._check_tmux', return_value=True):
                with patch('trustmux._ctl._check_tls', return_value=True):
                    with patch('trustmux._ctl.subprocess.run'):
                        with patch('trustmux._ctl._ts_host', return_value=''):
                            self.assertEqual(ctl.cmd_start('serve'), 1)

    def test_returns_1_when_ts_serve_fails(self):
        with patch('trustmux._ctl._pid', return_value=None):
            with patch('trustmux._ctl._check_tmux', return_value=True):
                with patch('trustmux._ctl._check_tls', return_value=True):
                    with patch('trustmux._ctl.subprocess.run'):
                        with patch('trustmux._ctl._ts_host', return_value='engawa.ts.net'):
                            with patch('trustmux._ctl._ensure_ts_serve', return_value=False):
                                self.assertEqual(ctl.cmd_start('serve'), 1)

    def test_serve_mode_success(self):
        with patch('trustmux._ctl._pid', return_value=None):
            with patch('trustmux._ctl._check_tmux', return_value=True):
                with patch('trustmux._ctl._check_tls', return_value=True):
                    with patch('trustmux._ctl.subprocess.run'):
                        with patch('trustmux._ctl._ts_host', return_value='engawa.ts.net'):
                            with patch('trustmux._ctl._ensure_ts_serve', return_value=True):
                                with patch('trustmux._ctl._launch', return_value=5678):
                                    self.assertEqual(ctl.cmd_start('serve'), 0)

    def test_start_local_success(self):
        with patch('trustmux._ctl._pid', return_value=None):
            with patch('trustmux._ctl._launch', return_value=5678):
                self.assertEqual(ctl.cmd_start('start-local'), 0)

    def test_start_direct_success(self):
        with patch('trustmux._ctl._pid', return_value=None):
            with patch('trustmux._ctl._check_tmux', return_value=True):
                with patch('trustmux._ctl._check_tls', return_value=True):
                    with patch('trustmux._ctl._launch', return_value=5678):
                        self.assertEqual(ctl.cmd_start('start-direct'), 0)

    def test_returns_1_when_launch_fails(self):
        with patch('trustmux._ctl._pid', return_value=None):
            with patch('trustmux._ctl._check_tmux', return_value=True):
                with patch('trustmux._ctl._check_tls', return_value=True):
                    with patch('trustmux._ctl.subprocess.run'):
                        with patch('trustmux._ctl._ts_host', return_value='engawa.ts.net'):
                            with patch('trustmux._ctl._ensure_ts_serve', return_value=True):
                                with patch('trustmux._ctl._launch', return_value=None):
                                    self.assertEqual(ctl.cmd_start('serve'), 1)

    def test_unknown_mode_returns_1(self):
        with patch('trustmux._ctl._pid', return_value=None):
            self.assertEqual(ctl.cmd_start('bogus'), 1)


# ---------------------------------------------------------------------------
# cmd_stop()
# ---------------------------------------------------------------------------

class TestCmdStop(unittest.TestCase):

    def setUp(self):
        self.inst = ctl.Instance('stoptest')
        self.inst.ensure_dirs()
        self.addCleanup(shutil.rmtree, self.inst.state, True)

    def test_not_running_returns_0(self):
        with patch('trustmux._ctl._pid', return_value=None):
            self.assertEqual(ctl.cmd_stop(inst=self.inst), 0)

    def test_kills_process_and_removes_pidfile(self):
        write_pid(self.inst, 4321)
        with patch('trustmux._ctl._pid', return_value=4321):
            with patch('trustmux._ctl.os.kill') as mock_kill:
                result = ctl.cmd_stop(inst=self.inst)
        self.assertEqual(result, 0)
        mock_kill.assert_called_once_with(4321, signal.SIGTERM)
        self.assertFalse(self.inst.pid_file.exists())

    def test_pidfile_mismatch_refuses_to_kill(self):
        write_pid(self.inst, 9999)
        with patch('trustmux._ctl._pid', return_value=4321):
            with patch('trustmux._ctl.os.kill') as mock_kill:
                result = ctl.cmd_stop(inst=self.inst)
        self.assertEqual(result, 1)
        mock_kill.assert_not_called()

    def test_no_pidfile_still_kills(self):
        with patch('trustmux._ctl._pid', return_value=4321):
            with patch('trustmux._ctl.os.kill') as mock_kill:
                result = ctl.cmd_stop(inst=self.inst)
        self.assertEqual(result, 0)
        mock_kill.assert_called_once_with(4321, signal.SIGTERM)

    def test_not_found_on_this_port_preserves_pidfile_of_live_other_daemon(self):
        # Nothing is on the requested port, but the pid file tracks a
        # genuinely alive daemon on some other port -- must not be deleted
        # just because the wrong port was asked about (regression: previously
        # unconditional, which threw away bookkeeping for a live daemon).
        write_pid(self.inst, 4321)
        with patch('trustmux._ctl._pid', return_value=None):
            with patch('trustmux._ctl.os.kill', return_value=None) as mock_kill:
                result = ctl.cmd_stop(4444, self.inst)
        self.assertEqual(result, 0)
        mock_kill.assert_called_once_with(4321, 0)  # liveness probe, not a real kill
        self.assertTrue(self.inst.pid_file.exists())

    def test_not_found_on_this_port_cleans_up_genuinely_dead_pidfile(self):
        # Same "not found" case, but the pid file's own pid is dead: this is
        # the genuinely-stale case, and should still be cleaned up as before.
        write_pid(self.inst, 4321)
        with patch('trustmux._ctl._pid', return_value=None):
            with patch('trustmux._ctl.os.kill', side_effect=ProcessLookupError):
                result = ctl.cmd_stop(4444, self.inst)
        self.assertEqual(result, 0)
        self.assertFalse(self.inst.pid_file.exists())


# ---------------------------------------------------------------------------
# cmd_status()
# ---------------------------------------------------------------------------

class TestCmdStatus(unittest.TestCase):

    def test_not_running(self):
        with patch('trustmux._ctl._pid', return_value=None):
            with patch('builtins.print') as mock_print:
                result = ctl.cmd_status()
        self.assertEqual(result, 0)
        mock_print.assert_called_once_with('trustmux not running')

    def test_running_with_tailscale_serve(self):
        port_str = f':{ctl.DEFAULT_PORT}'
        with patch('trustmux._ctl._pid', return_value=1234):
            with patch('trustmux._ctl.subprocess.check_output',
                       return_value=f'something {port_str} here'):
                with patch('trustmux._ctl._ts_host', return_value='engawa.ts.net'):
                    with patch('builtins.print') as mock_print:
                        result = ctl.cmd_status()
        self.assertEqual(result, 0)
        printed = ' '.join(str(c) for c in mock_print.call_args_list)
        self.assertIn('https://engawa.ts.net', printed)

    def test_running_direct_uses_daemon_reported_url(self):
        import subprocess as sp
        info = {'host': '0.0.0.0', 'port': 3389, 'scheme': 'https'}
        with patch('trustmux._ctl.daemon_info', return_value=info):
            with patch('trustmux._ctl._pid', return_value=1234):
                with patch('trustmux._ctl._lan_ip', return_value='192.168.1.9'):
                    with patch('trustmux._ctl.subprocess.check_output',
                               side_effect=sp.CalledProcessError(1, 'ts')):
                        with patch('builtins.print') as mock_print:
                            result = ctl.cmd_status()
        self.assertEqual(result, 0)
        printed = ' '.join(str(c) for c in mock_print.call_args_list)
        self.assertIn('https://192.168.1.9:3389/', printed)
        self.assertIn('port 3389', printed)

    def test_running_local_reports_loopback_http(self):
        import subprocess as sp
        info = {'host': '127.0.0.1', 'port': 3389, 'scheme': 'http'}
        with patch('trustmux._ctl.daemon_info', return_value=info):
            with patch('trustmux._ctl._pid', return_value=1234):
                with patch('trustmux._ctl.subprocess.check_output',
                           side_effect=sp.CalledProcessError(1, 'ts')):
                    with patch('builtins.print') as mock_print:
                        result = ctl.cmd_status()
        self.assertEqual(result, 0)
        printed = ' '.join(str(c) for c in mock_print.call_args_list)
        self.assertIn('http://localhost:3389/', printed)


# ---------------------------------------------------------------------------
# _install_hook() — enable
# ---------------------------------------------------------------------------

class TestInstallHook(unittest.TestCase):

    def test_no_op_when_file_missing(self):
        with tempfile.TemporaryDirectory() as td:
            dest = Path(td) / 'nonexistent'
            enable._install_hook(dest)
            self.assertFalse(dest.exists())

    def test_adds_hook_to_existing_file(self):
        with tempfile.NamedTemporaryFile(mode='w', suffix='.profile',
                                         delete=False) as f:
            f.write('# existing content\n')
            fpath = Path(f.name)
        try:
            enable._install_hook(fpath)
            content = fpath.read_text()
            self.assertIn('trustmux start', content)
            self.assertIn('# existing content', content)
        finally:
            fpath.unlink()

    def test_idempotent_does_not_duplicate_hook(self):
        with tempfile.NamedTemporaryFile(mode='w', suffix='.profile',
                                         delete=False) as f:
            f.write('trustmux start 2>/dev/null || true\n')
            fpath = Path(f.name)
        try:
            enable._install_hook(fpath)
            enable._install_hook(fpath)
            content = fpath.read_text()
            self.assertEqual(content.count('trustmux start'), 1)
        finally:
            fpath.unlink()


# ---------------------------------------------------------------------------
# _remove_hook() — disable
# ---------------------------------------------------------------------------

class TestRemoveHook(unittest.TestCase):

    def test_no_op_when_file_missing(self):
        with tempfile.TemporaryDirectory() as td:
            dest = Path(td) / 'nonexistent'
            disable._remove_hook(dest)   # must not raise

    def test_removes_hook_lines(self):
        with tempfile.NamedTemporaryFile(mode='w', suffix='.profile',
                                         delete=False) as f:
            f.write('# preamble\ntrustmux start 2>/dev/null || true\n# after\n')
            fpath = Path(f.name)
        try:
            disable._remove_hook(fpath)
            content = fpath.read_text()
            self.assertNotIn('trustmux start 2>/dev/null', content)
            self.assertIn('# preamble', content)
            self.assertIn('# after', content)
        finally:
            fpath.unlink()

    def test_no_change_when_hook_absent(self):
        original = '# just a comment\nexport PATH=$PATH:/usr/local/bin\n'
        with tempfile.NamedTemporaryFile(mode='w', suffix='.profile',
                                         delete=False) as f:
            f.write(original)
            fpath = Path(f.name)
        try:
            disable._remove_hook(fpath)
            self.assertEqual(fpath.read_text(), original)
        finally:
            fpath.unlink()

    def test_no_op_on_non_writable_file(self):
        with tempfile.NamedTemporaryFile(mode='w', suffix='.profile',
                                         delete=False) as f:
            f.write('trustmux start\n')
            fpath = Path(f.name)
        try:
            with patch('trustmux._disable.os.access', return_value=False):
                disable._remove_hook(fpath)
            self.assertIn('trustmux start', fpath.read_text())
        finally:
            fpath.unlink()


# ---------------------------------------------------------------------------
# enable.main() and disable.main() — integration-level
# ---------------------------------------------------------------------------

class TestEnableMain(unittest.TestCase):

    def test_exits_1_when_setup_fails(self):
        with patch('trustmux._enable.cmd_setup', return_value=1):
            with self.assertRaises(SystemExit) as cm:
                enable.main()
        self.assertEqual(cm.exception.code, 1)

    def test_runs_through_on_success(self):
        with patch('trustmux._enable.cmd_setup', return_value=0):
            with patch('trustmux._enable.cmd_start', return_value=0):
                with patch('trustmux._enable._LOGIN_FILES', []):
                    with patch('trustmux._paths.Instance.tokens_file') as tf:
                        tf.exists.return_value = True
                        tf.stat.return_value = MagicMock(st_size=100)
                        enable.main()   # should not raise


class TestDisableMain(unittest.TestCase):

    def test_runs_without_error(self):
        with patch('trustmux._disable.cmd_stop', return_value=0):
            with patch('trustmux._disable._LOGIN_FILES', []):
                disable.main()   # should not raise


# ---------------------------------------------------------------------------
# _peer_acl_allows_tcp() — tailnet ACL preflight
# ---------------------------------------------------------------------------

def _netmap(rules, self_addrs=("100.93.98.28/32",)):
    """Build a minimal netmap JSON blob for the ACL preflight tests."""
    nm = {"PacketFilter": rules}
    if self_addrs is not None:
        nm["SelfNode"] = {"Addresses": list(self_addrs)}
    return json.dumps(nm)


class TestPeerAclAllowsTcp(unittest.TestCase):

    def _patch_netmap(self, output):
        return patch('trustmux._ctl.subprocess.check_output', return_value=output)

    def test_rule_allows_port_and_proto(self):
        rules = [{
            "IPProto": [6],
            "Dsts": [{"Net": "100.93.98.28/32", "Ports": {"First": 443, "Last": 443}}],
        }]
        with self._patch_netmap(_netmap(rules)):
            self.assertTrue(ctl._peer_acl_allows_tcp(443))

    def test_rule_allows_port_range(self):
        rules = [{
            "IPProto": [6],
            "Dsts": [{"Net": "100.93.98.28/32", "Ports": {"First": 1, "Last": 65535}}],
        }]
        with self._patch_netmap(_netmap(rules)):
            self.assertTrue(ctl._peer_acl_allows_tcp(443))

    def test_empty_iproto_means_all_protocols(self):
        rules = [{
            "IPProto": [],
            "Dsts": [{"Net": "100.93.98.28/32", "Ports": {"First": 443, "Last": 443}}],
        }]
        with self._patch_netmap(_netmap(rules)):
            self.assertTrue(ctl._peer_acl_allows_tcp(443))

    def test_no_rule_covers_port(self):
        rules = [{
            "IPProto": [6],
            "Dsts": [{"Net": "100.93.98.28/32", "Ports": {"First": 22, "Last": 22}}],
        }]
        with self._patch_netmap(_netmap(rules)):
            self.assertFalse(ctl._peer_acl_allows_tcp(443))

    def test_wrong_protocol_rejected(self):
        rules = [{
            "IPProto": [17],  # UDP only
            "Dsts": [{"Net": "100.93.98.28/32", "Ports": {"First": 443, "Last": 443}}],
        }]
        with self._patch_netmap(_netmap(rules)):
            self.assertFalse(ctl._peer_acl_allows_tcp(443))

    def test_rule_for_different_device_rejected(self):
        rules = [{
            "IPProto": [6],
            "Dsts": [{"Net": "100.64.0.99/32", "Ports": {"First": 443, "Last": 443}}],
        }]
        with self._patch_netmap(_netmap(rules)):
            self.assertFalse(ctl._peer_acl_allows_tcp(443))

    def test_cidr_rule_covers_device_ip(self):
        # CIDR block that contains the device — default autogroup:member ACL
        # uses 100.64.0.0/10 rather than a per-host /32.
        rules = [{
            "IPProto": [6],
            "Dsts": [{"Net": "100.64.0.0/10", "Ports": {"First": 443, "Last": 443}}],
        }]
        with self._patch_netmap(_netmap(rules)):  # device is 100.93.98.28
            self.assertTrue(ctl._peer_acl_allows_tcp(443))

    def test_cidr_rule_does_not_cover_device_ip(self):
        rules = [{
            "IPProto": [6],
            "Dsts": [{"Net": "10.0.0.0/8", "Ports": {"First": 443, "Last": 443}}],
        }]
        with self._patch_netmap(_netmap(rules)):
            self.assertFalse(ctl._peer_acl_allows_tcp(443))

    def test_accepts_match_when_self_ips_unknown(self):
        # If SelfNode.Addresses is absent, fall back to "any net" matching so
        # we don't false-positive a warning.
        rules = [{
            "IPProto": [6],
            "Dsts": [{"Net": "100.64.0.99/32", "Ports": {"First": 443, "Last": 443}}],
        }]
        nm = json.dumps({"PacketFilter": rules})
        with patch('trustmux._ctl.subprocess.check_output', return_value=nm):
            self.assertTrue(ctl._peer_acl_allows_tcp(443))

    def test_tailscale_missing_returns_none(self):
        with patch('trustmux._ctl.subprocess.check_output',
                   side_effect=FileNotFoundError):
            self.assertIsNone(ctl._peer_acl_allows_tcp(443))

    def test_malformed_json_returns_none(self):
        with patch('trustmux._ctl.subprocess.check_output',
                   return_value='not json'):
            self.assertIsNone(ctl._peer_acl_allows_tcp(443))

    def test_missing_packet_filter_returns_none(self):
        with patch('trustmux._ctl.subprocess.check_output',
                   return_value='{}'):
            self.assertIsNone(ctl._peer_acl_allows_tcp(443))


# ---------------------------------------------------------------------------
# warn_if_peer_blocked()
# ---------------------------------------------------------------------------

class TestWarnIfPeerBlocked(unittest.TestCase):

    def test_silent_when_reachable(self):
        import io
        buf = io.StringIO()
        with patch('trustmux._ctl._peer_acl_allows_tcp', return_value=True):
            ctl.warn_if_peer_blocked(443, stream=buf)
        self.assertEqual(buf.getvalue(), "")

    def test_silent_when_unknown(self):
        import io
        buf = io.StringIO()
        with patch('trustmux._ctl._peer_acl_allows_tcp', return_value=None):
            ctl.warn_if_peer_blocked(443, stream=buf)
        self.assertEqual(buf.getvalue(), "")

    def test_warns_when_blocked(self):
        import io
        buf = io.StringIO()
        with patch('trustmux._ctl._peer_acl_allows_tcp', return_value=False):
            ctl.warn_if_peer_blocked(443, stream=buf)
        msg = buf.getvalue()
        self.assertIn("warning", msg)
        self.assertIn("tcp:443", msg)
        self.assertIn("ERR_NETWORK_CHANGED", msg)
        # Mentions both ACL formats so users on either can self-serve.
        self.assertIn("grants", msg)
        self.assertIn("acls", msg)


# ---------------------------------------------------------------------------
# main() -- help discoverability
# ---------------------------------------------------------------------------

class TestMainHelp(unittest.TestCase):
    """`trustmux help` is a hidden alias for -h/--help (git/docker/kubectl-style),
    added because a plain 'help' guess otherwise hits argparse's terse
    "invalid choice" error instead of the actual help text."""

    def _run(self, argv):
        with patch('trustmux._ctl._refuse_root'), \
             patch('trustmux._ctl.sys.argv', ['trustmux'] + argv):
            with self.assertRaises(SystemExit) as cm:
                ctl.main()
            return cm.exception.code

    def test_help_subcommand_exits_zero(self):
        self.assertEqual(self._run(['help']), 0)

    def test_no_args_exits_one(self):
        self.assertEqual(self._run([]), 1)

    def test_help_subcommand_prints_full_help(self):
        with patch('trustmux._ctl._refuse_root'), \
             patch('trustmux._ctl.sys.argv', ['trustmux', 'help']), \
             patch('trustmux._ctl.argparse.ArgumentParser.print_help') as mock_print:
            with self.assertRaises(SystemExit):
                ctl.main()
            mock_print.assert_called_once()


# ---------------------------------------------------------------------------
# Port selection: --port / $TRUSTMUX_PORT / running daemon / default
# ---------------------------------------------------------------------------

class TestValidPort(unittest.TestCase):

    def test_accepts_int_and_numeric_string(self):
        self.assertEqual(ctl._valid_port(3389), 3389)
        self.assertEqual(ctl._valid_port('3389'), 3389)

    def test_rejects_out_of_range_and_junk(self):
        for bad in (0, -1, 65536, 'abc', '', None, 3.5j):
            self.assertIsNone(ctl._valid_port(bad), bad)


class TestResolvePort(unittest.TestCase):

    def setUp(self):
        self.env = patch.dict(os.environ, {}, clear=False)
        self.env.start()
        os.environ.pop(ctl.PORT_ENV, None)

    def tearDown(self):
        self.env.stop()

    def test_default_when_nothing_set(self):
        self.assertEqual(ctl.resolve_port(), ctl.DEFAULT_PORT)

    def test_explicit_wins_over_env_and_daemon(self):
        os.environ[ctl.PORT_ENV] = '4444'
        with patch('trustmux._ctl.daemon_info', return_value={'port': 5555}):
            self.assertEqual(ctl.resolve_port(3389), 3389)

    def test_env_wins_over_daemon(self):
        os.environ[ctl.PORT_ENV] = '4444'
        with patch('trustmux._ctl.daemon_info', return_value={'port': 5555}):
            self.assertEqual(ctl.resolve_port(), 4444)

    def test_falls_back_to_running_daemon(self):
        with patch('trustmux._ctl.daemon_info', return_value={'port': 5555}):
            self.assertEqual(ctl.resolve_port(), 5555)

    def test_ignores_nonsense_from_daemon(self):
        with patch('trustmux._ctl.daemon_info', return_value={'port': 'wat'}):
            self.assertEqual(ctl.resolve_port(), ctl.DEFAULT_PORT)

    def test_blank_env_is_ignored(self):
        os.environ[ctl.PORT_ENV] = '   '
        self.assertEqual(ctl.resolve_port(), ctl.DEFAULT_PORT)

    def test_invalid_env_exits_2(self):
        os.environ[ctl.PORT_ENV] = '99999'
        with patch('trustmux._ctl.sys.stderr'):
            with self.assertRaises(SystemExit) as cm:
                ctl.resolve_port()
        self.assertEqual(cm.exception.code, 2)

    def test_invalid_explicit_exits_2(self):
        with patch('trustmux._ctl.sys.stderr'):
            with self.assertRaises(SystemExit) as cm:
                ctl.resolve_port(0)
        self.assertEqual(cm.exception.code, 2)


class TestPortOpt(unittest.TestCase):

    def test_empty_for_default(self):
        self.assertEqual(ctl.port_opt(ctl.DEFAULT_PORT), '')

    def test_flag_for_custom(self):
        self.assertEqual(ctl.port_opt(3389), ' --port 3389')


class TestDaemonInfo(unittest.TestCase):
    """setUpModule stubs daemon_info out for the rest of the suite; these cases
    exercise the real implementation against a real unix socket."""

    def setUp(self):
        _no_daemon.stop()
        self.addCleanup(_no_daemon.start)
        self.inst = ctl.Instance('infotest')
        self.inst.ensure_dirs()
        self.addCleanup(shutil.rmtree, self.inst.state, True)
        self.sock_path = self.inst.sock

    def _serve_once(self, reply: bytes) -> None:
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(str(self.sock_path))
        srv.listen(1)
        self.addCleanup(srv.close)

        def handle():
            conn, _ = srv.accept()
            with conn:
                conn.recv(4096)
                conn.sendall(reply)

        t = threading.Thread(target=handle, daemon=True)
        t.start()
        self.addCleanup(t.join, 5)

    def test_none_when_socket_absent(self):
        self.assertIsNone(ctl.daemon_info(self.inst))

    def test_returns_listener_details(self):
        self._serve_once(b'{"pid": 42, "host": "0.0.0.0", "port": 3389, "scheme": "https"}\n')
        info = ctl.daemon_info(self.inst)
        self.assertEqual(info['port'], 3389)
        self.assertEqual(info['scheme'], 'https')

    def test_none_on_error_reply(self):
        self._serve_once(b'{"error": "unknown action"}\n')
        self.assertIsNone(ctl.daemon_info(self.inst))

    def test_none_on_garbage_reply(self):
        self._serve_once(b'not json\n')
        self.assertIsNone(ctl.daemon_info(self.inst))


class TestDirectUrl(unittest.TestCase):

    def test_loopback_bind_is_localhost(self):
        url = ctl.direct_url(3389, {'host': '127.0.0.1', 'scheme': 'http'})
        self.assertEqual(url, 'http://localhost:3389/')

    def test_wildcard_bind_uses_lan_ip(self):
        with patch('trustmux._ctl._lan_ip', return_value='192.168.1.9'):
            url = ctl.direct_url(3389, {'host': '0.0.0.0', 'scheme': 'https'})
        self.assertEqual(url, 'https://192.168.1.9:3389/')

    def test_defaults_to_https_when_daemon_silent(self):
        with patch('trustmux._ctl._lan_ip', return_value='192.168.1.9'):
            self.assertEqual(ctl.direct_url(7432, {}), 'https://192.168.1.9:7432/')

    def test_live_daemon_port_wins_over_caller(self):
        url = ctl.direct_url(4444, {'host': '127.0.0.1', 'port': 3389, 'scheme': 'http'})
        self.assertEqual(url, 'http://localhost:3389/')


class TestLaunchPort(unittest.TestCase):

    def setUp(self):
        self.inst = ctl.Instance('launchtest')
        self.inst.ensure_dirs()
        self.addCleanup(shutil.rmtree, self.inst.state, True)

    def _launch(self, port, inst):
        with patch('trustmux._ctl.subprocess.Popen') as mock_popen:
            mock_popen.return_value.poll.return_value = None   # still alive
            with patch('trustmux._ctl.time.sleep'):
                with patch('trustmux._ctl._pid', return_value=4321):
                    ctl._launch(port, ['--host', '127.0.0.1'], inst)
        return mock_popen

    def test_passes_resolved_port_to_daemon(self):
        mock_popen = self._launch(3389, self.inst)
        argv = mock_popen.call_args[0][0]
        self.assertIn('--port', argv)
        self.assertEqual(argv[argv.index('--port') + 1], '3389')
        # Never handed to a shell.
        self.assertNotIn('shell', mock_popen.call_args.kwargs)

    def test_passes_instance_to_daemon(self):
        mock_popen = self._launch(3389, self.inst)
        argv = mock_popen.call_args[0][0]
        self.assertEqual(argv[argv.index('--instance') + 1], 'launchtest')

    def test_writes_pid_to_this_instance(self):
        self._launch(3389, self.inst)
        self.assertTrue(self.inst.pid_file.exists())

    def test_reports_failure_when_the_daemon_dies_on_startup(self):
        # Port conflicts are easy to hit once several instances are in play;
        # a child that has already exited must not be reported as started.
        with patch('trustmux._ctl.subprocess.Popen') as mock_popen:
            mock_popen.return_value.poll.return_value = 1   # already exited
            with patch('trustmux._ctl.time.sleep'):
                with patch('trustmux._ctl._pid', return_value=4321):
                    result = ctl._launch(3389, ['--host', '127.0.0.1'], self.inst)
        self.assertIsNone(result)
        self.assertFalse(self.inst.pid_file.exists())


class TestEnsureTsServePort(unittest.TestCase):

    def test_serves_the_requested_port(self):
        with patch('trustmux._ctl.subprocess.check_output', return_value='nothing'):
            with patch('trustmux._ctl.subprocess.run') as mock_run:
                mock_run.return_value = MagicMock(returncode=0)
                self.assertTrue(ctl._ensure_ts_serve(3389))
        self.assertEqual(mock_run.call_args[0][0],
                         ['tailscale', 'serve', '--bg', '3389'])


class TestCmdStartPort(unittest.TestCase):

    def test_start_local_launches_on_requested_port(self):
        with patch('trustmux._ctl._pid', return_value=None):
            with patch('trustmux._ctl._launch', return_value=1234) as mock_launch:
                with patch('builtins.print'):
                    self.assertEqual(ctl.cmd_start('start-local', 3389), 0)
        self.assertEqual(mock_launch.call_args[0][0], 3389)

    def test_refuses_when_something_already_owns_that_port(self):
        with patch('trustmux._ctl._pid', return_value=999) as mock_pid:
            with patch('builtins.print'):
                self.assertEqual(ctl.cmd_start('start-local', 3389), 1)
        mock_pid.assert_called_once_with(3389, ctl.Instance())


class TestCmdStopPort(unittest.TestCase):

    def test_stops_pid_found_on_requested_port(self):
        inst = ctl.Instance('stopport')
        inst.ensure_dirs()
        self.addCleanup(shutil.rmtree, inst.state, True)
        with patch('trustmux._ctl._pid', return_value=1234) as mock_pid:
            with patch('trustmux._ctl.os.kill') as mock_kill:
                with patch('builtins.print'):
                    self.assertEqual(ctl.cmd_stop(3389, inst), 0)
        mock_pid.assert_called_once_with(3389, inst)
        mock_kill.assert_called_once_with(1234, signal.SIGTERM)


# ---------------------------------------------------------------------------
# Login hook carries the port
# ---------------------------------------------------------------------------

class TestHookLine(unittest.TestCase):

    def test_default_port_keeps_legacy_line(self):
        self.assertEqual(enable.hook_line(ctl.DEFAULT_PORT), enable._HOOK)

    def test_custom_port_is_carried(self):
        self.assertEqual(enable.hook_line(3389),
                         'trustmux start --port 3389 2>/dev/null || true\n')

    def test_is_hook_line_matches_both_formats(self):
        self.assertTrue(enable.is_hook_line('trustmux start 2>/dev/null || true'))
        self.assertTrue(enable.is_hook_line('trustmux start --port 3389 2>/dev/null || true'))
        self.assertTrue(enable.is_hook_line('trustmux-ctl start'))
        self.assertFalse(enable.is_hook_line('# trustmux is great'))
        self.assertFalse(enable.is_hook_line('alias tm="trustmux status"'))


class TestInstallHookPort(unittest.TestCase):

    def _profile(self, content):
        f = tempfile.NamedTemporaryFile(mode='w', suffix='.profile', delete=False)
        f.write(content)
        f.close()
        self.addCleanup(Path(f.name).unlink)
        return Path(f.name)

    def test_appends_hook_with_port(self):
        p = self._profile('# existing\n')
        enable._install_hook(p, enable.hook_line(3389))
        self.assertIn('trustmux start --port 3389 2>/dev/null', p.read_text())

    def test_rewrites_existing_hook_with_new_port(self):
        p = self._profile('# top\ntrustmux start 2>/dev/null || true\n# bottom\n')
        enable._install_hook(p, enable.hook_line(3389))
        text = p.read_text()
        self.assertEqual(text.count('trustmux start'), 1)
        self.assertIn('--port 3389', text)
        self.assertIn('# top', text)
        self.assertIn('# bottom', text)

    def test_disable_removes_hook_with_port(self):
        p = self._profile('# top\ntrustmux start --port 3389 2>/dev/null || true\n')
        disable._remove_hook(p)
        self.assertNotIn('trustmux start', p.read_text())
        self.assertIn('# top', p.read_text())


# ---------------------------------------------------------------------------
# Instances: isolation, listing, per-instance login hooks
# ---------------------------------------------------------------------------

class TestInstanceIsolation(unittest.TestCase):

    def test_resolve_port_asks_the_named_instance(self):
        work = ctl.Instance('work')
        with patch('trustmux._ctl.daemon_info') as info:
            info.return_value = {'port': 3389}
            self.assertEqual(ctl.resolve_port(inst=work), 3389)
        info.assert_called_once_with(work)

    def test_status_and_stop_address_the_named_instance(self):
        work = ctl.Instance('work')
        work.ensure_dirs()
        self.addCleanup(shutil.rmtree, work.state, True)
        with patch('trustmux._ctl._pid', return_value=None) as mock_pid:
            with patch('builtins.print'):
                ctl.cmd_status(3389, work)
        self.assertEqual(mock_pid.call_args[0][1], work)


class TestCmdList(unittest.TestCase):

    def setUp(self):
        self.made = []
        for name in ('default', 'work'):
            inst = ctl.Instance(name)
            inst.ensure_dirs()
            self.made.append(inst)
            self.addCleanup(shutil.rmtree, inst.state, True)

    def _run(self):
        with patch('builtins.print') as mock_print:
            rc = ctl.cmd_list()
        return rc, ' '.join(str(c) for c in mock_print.call_args_list)

    def test_lists_every_instance(self):
        rc, out = self._run()
        self.assertEqual(rc, 0)
        self.assertIn('default', out)
        self.assertIn('work', out)

    def test_reports_a_running_instance_with_its_port(self):
        def fake_info(inst):
            return {'port': 3389, 'scheme': 'https'} if inst.name == 'work' else None
        with patch('trustmux._ctl.daemon_info', side_effect=fake_info):
            with patch('trustmux._ctl._pid', return_value=4321):
                rc, out = self._run()
        self.assertEqual(rc, 0)
        self.assertIn('3389', out)
        self.assertIn('running (pid 4321)', out)

    def test_reports_stopped_instances(self):
        with patch('trustmux._ctl._recorded_pid', return_value=None):
            rc, out = self._run()
        self.assertIn('stopped', out)


class TestPerInstanceHooks(unittest.TestCase):

    def _profile(self, content=''):
        f = tempfile.NamedTemporaryFile(mode='w', suffix='.profile', delete=False)
        f.write(content)
        f.close()
        self.addCleanup(Path(f.name).unlink, True)
        return Path(f.name)

    def test_hook_line_carries_instance_and_port(self):
        line = enable.hook_line(3389, ctl.Instance('work'))
        self.assertEqual(line,
                         'trustmux start --instance work --port 3389 2>/dev/null || true\n')

    def test_default_instance_keeps_the_legacy_line(self):
        self.assertEqual(enable.hook_line(ctl.DEFAULT_PORT, ctl.Instance()), enable._HOOK)

    def test_second_instance_appends_rather_than_replacing(self):
        p = self._profile('# top\n')
        enable._install_hook(p, enable.hook_line(7432, ctl.Instance()), ctl.Instance())
        enable._install_hook(p, enable.hook_line(3389, ctl.Instance('work')),
                             ctl.Instance('work'))
        text = p.read_text()
        self.assertEqual(text.count('trustmux start'), 2)
        self.assertIn('--instance work', text)

    def test_reenabling_one_instance_rewrites_only_its_line(self):
        p = self._profile('')
        enable._install_hook(p, enable.hook_line(7432, ctl.Instance()), ctl.Instance())
        enable._install_hook(p, enable.hook_line(3389, ctl.Instance('work')),
                             ctl.Instance('work'))
        enable._install_hook(p, enable.hook_line(9999, ctl.Instance('work')),
                             ctl.Instance('work'))
        text = p.read_text()
        self.assertEqual(text.count('trustmux start'), 2)
        self.assertIn('--port 9999', text)
        self.assertNotIn('--port 3389', text)

    def test_disable_removes_only_that_instance(self):
        p = self._profile('')
        enable._install_hook(p, enable.hook_line(7432, ctl.Instance()), ctl.Instance())
        enable._install_hook(p, enable.hook_line(3389, ctl.Instance('work')),
                             ctl.Instance('work'))
        disable._remove_hook(p, ctl.Instance('work'))
        text = p.read_text()
        self.assertNotIn('--instance work', text)
        self.assertIn('trustmux start 2>/dev/null', text)

    def test_disabling_default_leaves_named_instances(self):
        p = self._profile('')
        enable._install_hook(p, enable.hook_line(7432, ctl.Instance()), ctl.Instance())
        enable._install_hook(p, enable.hook_line(3389, ctl.Instance('work')),
                             ctl.Instance('work'))
        disable._remove_hook(p, ctl.Instance())
        text = p.read_text()
        self.assertIn('--instance work', text)
        self.assertNotIn('trustmux start 2>/dev/null', text)


# ---------------------------------------------------------------------------
# serve mode is singular: only one daemon can own the tailnet's :443
# ---------------------------------------------------------------------------

class TestServeModeGuard(unittest.TestCase):
    """`tailscale serve` publishes on the tailnet's port 443. A second
    `tailscale serve --bg` silently replaces the first mapping, so a named
    instance must not be allowed into serve mode at all. --port does not help:
    it moves only the loopback backend, not the tailnet-facing port."""

    def _stderr(self):
        import io
        return io.StringIO()

    def test_default_instance_may_use_serve(self):
        self.assertTrue(ctl.can_use_serve(ctl.Instance(), self._stderr()))

    def test_named_instance_may_not(self):
        buf = self._stderr()
        self.assertFalse(ctl.can_use_serve(ctl.Instance('work'), buf))
        msg = buf.getvalue()
        self.assertIn('work', msg)
        self.assertIn('443', msg)
        # Points at the modes that actually work for a second instance.
        self.assertIn('start-direct --instance work', msg)
        self.assertIn('start-local --instance work', msg)

    def test_cmd_start_serve_refuses_named_instance(self):
        with patch('trustmux._ctl._launch') as mock_launch:
            with patch('trustmux._ctl._ensure_ts_serve') as mock_serve:
                with patch('trustmux._ctl.sys.stderr', self._stderr()):
                    rc = ctl.cmd_start('serve', 7432, ctl.Instance('work'))
        self.assertEqual(rc, 1)
        mock_launch.assert_not_called()
        # Crucially, the existing serve mapping is never touched.
        mock_serve.assert_not_called()

    def test_refusal_happens_before_any_probing(self):
        # The refusal is certain, so it must not first query a daemon or
        # fork lsof.
        with patch('trustmux._ctl._pid') as mock_pid:
            with patch('trustmux._ctl.daemon_info') as mock_info:
                with patch('trustmux._ctl.sys.stderr', self._stderr()):
                    ctl.cmd_start('serve', None, ctl.Instance('work'))
        mock_pid.assert_not_called()
        mock_info.assert_not_called()

    def test_named_instance_may_still_use_direct_and_local(self):
        for mode in ('start-direct', 'start-local'):
            with self.subTest(mode=mode):
                with patch('trustmux._ctl._launch', return_value=1234) as mock_launch:
                    with patch('trustmux._ctl._check_tmux', return_value=True):
                        with patch('trustmux._ctl._check_tls', return_value=True):
                            with patch('trustmux._ctl._pid', return_value=None):
                                with patch('builtins.print'):
                                    rc = ctl.cmd_start(mode, 3389, ctl.Instance('work'))
                self.assertEqual(rc, 0)
                mock_launch.assert_called_once()

    def test_restart_refuses_before_stopping_anything(self):
        # restart brings the daemon back in serve mode; refusing only at the
        # start step would leave a running named instance stopped.
        with patch('trustmux._ctl._refuse_root'), \
             patch('trustmux._ctl.sys.argv',
                   ['trustmux', 'restart', '--instance', 'work']), \
             patch('trustmux._ctl.cmd_stop') as mock_stop, \
             patch('trustmux._ctl.cmd_start') as mock_start, \
             patch('trustmux._ctl.sys.stderr', self._stderr()):
            with self.assertRaises(SystemExit) as cm:
                ctl.main()
        self.assertEqual(cm.exception.code, 1)
        mock_stop.assert_not_called()
        mock_start.assert_not_called()

    def test_cmd_setup_refuses_named_instance(self):
        with patch('trustmux._ctl._ensure_ts_serve') as mock_serve:
            with patch('trustmux._ctl.sys.stderr', self._stderr()):
                rc = ctl.cmd_setup(inst=ctl.Instance('work'))
        self.assertEqual(rc, 1)
        mock_serve.assert_not_called()

    def test_enable_refuses_named_instance_without_looping(self):
        with patch('trustmux._enable.cmd_setup') as mock_setup:
            with patch('trustmux._enable._install_hook') as mock_hook:
                with patch('trustmux._ctl.sys.stderr', self._stderr()):
                    with patch('sys.stderr', self._stderr()):
                        with self.assertRaises(SystemExit) as cm:
                            enable.main(3389, ctl.Instance('work'))
        self.assertEqual(cm.exception.code, 1)
        # Refused up front: no setup attempt, no hook written.
        mock_setup.assert_not_called()
        mock_hook.assert_not_called()

    def test_enable_still_works_for_the_default_instance(self):
        with patch('trustmux._enable.cmd_setup', return_value=0) as mock_setup:
            with patch('trustmux._enable.cmd_start', return_value=0):
                with patch('trustmux._enable._install_hook'):
                    with patch('trustmux._paths.Instance.tokens_file'):
                        with patch('builtins.print'):
                            enable.main(ctl.DEFAULT_PORT, ctl.Instance())
        mock_setup.assert_called_once()


if __name__ == '__main__':
    unittest.main()
