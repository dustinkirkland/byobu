"""Tests for trustmux._ctl, _enable, _disable."""

import json
import os
import signal
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, call, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import trustmux._ctl as ctl
import trustmux._enable as enable
import trustmux._disable as disable


# ---------------------------------------------------------------------------
# _pid()
# ---------------------------------------------------------------------------

class TestPid(unittest.TestCase):

    def test_lsof_returns_pid(self):
        with patch('trustmux._ctl.subprocess.check_output', return_value='1234\n'):
            self.assertEqual(ctl._pid(), 1234)

    def test_lsof_multiple_lines_uses_first(self):
        with patch('trustmux._ctl.subprocess.check_output', return_value='1234\n5678\n'):
            self.assertEqual(ctl._pid(), 1234)

    def _mock_pidfile(self, exists=False, content=''):
        m = MagicMock()
        m.exists.return_value = exists
        m.read_text.return_value = content
        return m

    def test_lsof_empty_output_falls_back_to_pidfile(self):
        with patch('trustmux._ctl.subprocess.check_output', return_value=''):
            with patch('trustmux._ctl.PIDFILE', self._mock_pidfile(exists=False)):
                self.assertIsNone(ctl._pid())

    def test_lsof_not_found_falls_back_to_pidfile(self):
        with patch('trustmux._ctl.subprocess.check_output',
                   side_effect=FileNotFoundError):
            with patch('trustmux._ctl.PIDFILE', self._mock_pidfile(exists=False)):
                self.assertIsNone(ctl._pid())

    def test_pidfile_fallback_live_process(self):
        import subprocess
        with patch('trustmux._ctl.subprocess.check_output',
                   side_effect=subprocess.CalledProcessError(1, 'lsof')):
            with patch('trustmux._ctl.PIDFILE',
                       self._mock_pidfile(exists=True, content='9999\n')):
                with patch('trustmux._ctl.os.kill', return_value=None):
                    self.assertEqual(ctl._pid(), 9999)

    def test_pidfile_fallback_dead_process(self):
        import subprocess
        with patch('trustmux._ctl.subprocess.check_output',
                   side_effect=subprocess.CalledProcessError(1, 'lsof')):
            with patch('trustmux._ctl.PIDFILE',
                       self._mock_pidfile(exists=True, content='9999\n')):
                with patch('trustmux._ctl.os.kill',
                           side_effect=ProcessLookupError):
                    self.assertIsNone(ctl._pid())

    def test_pidfile_fallback_invalid_content(self):
        import subprocess
        with patch('trustmux._ctl.subprocess.check_output',
                   side_effect=subprocess.CalledProcessError(1, 'lsof')):
            with patch('trustmux._ctl.PIDFILE',
                       self._mock_pidfile(exists=True, content='not-a-pid\n')):
                self.assertIsNone(ctl._pid())


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
        port_str = f':{ctl.PORT}'
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

    def _mock_pidfile(self, exists=False, content=''):
        m = MagicMock()
        m.exists.return_value = exists
        m.read_text.return_value = content
        return m

    def test_not_running_returns_0(self):
        with patch('trustmux._ctl._pid', return_value=None):
            with patch('trustmux._ctl.PIDFILE', self._mock_pidfile(exists=False)):
                self.assertEqual(ctl.cmd_stop(), 0)

    def test_kills_process_and_removes_pidfile(self):
        with patch('trustmux._ctl._pid', return_value=4321):
            with patch('trustmux._ctl.PIDFILE',
                       self._mock_pidfile(exists=True, content='4321')):
                with patch('trustmux._ctl.os.kill') as mock_kill:
                    result = ctl.cmd_stop()
        self.assertEqual(result, 0)
        mock_kill.assert_called_once_with(4321, signal.SIGTERM)

    def test_pidfile_mismatch_refuses_to_kill(self):
        with patch('trustmux._ctl._pid', return_value=4321):
            with patch('trustmux._ctl.PIDFILE',
                       self._mock_pidfile(exists=True, content='9999')):
                with patch('trustmux._ctl.os.kill') as mock_kill:
                    result = ctl.cmd_stop()
        self.assertEqual(result, 1)
        mock_kill.assert_not_called()

    def test_no_pidfile_still_kills(self):
        with patch('trustmux._ctl._pid', return_value=4321):
            with patch('trustmux._ctl.PIDFILE', self._mock_pidfile(exists=False)):
                with patch('trustmux._ctl.os.kill') as mock_kill:
                    result = ctl.cmd_stop()
        self.assertEqual(result, 0)
        mock_kill.assert_called_once_with(4321, signal.SIGTERM)


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
        port_str = f':{ctl.PORT}'
        with patch('trustmux._ctl._pid', return_value=1234):
            with patch('trustmux._ctl.subprocess.check_output',
                       return_value=f'something {port_str} here'):
                with patch('trustmux._ctl._ts_host', return_value='engawa.ts.net'):
                    with patch('builtins.print') as mock_print:
                        result = ctl.cmd_status()
        self.assertEqual(result, 0)
        printed = ' '.join(str(c) for c in mock_print.call_args_list)
        self.assertIn('https://engawa.ts.net', printed)

    def test_running_direct_http(self):
        import subprocess as sp
        with patch('trustmux._ctl._pid', return_value=1234):
            with patch('trustmux._ctl.subprocess.check_output',
                       side_effect=[sp.CalledProcessError(1, 'ts'), '100.64.0.1\n']):
                with patch('builtins.print') as mock_print:
                    result = ctl.cmd_status()
        self.assertEqual(result, 0)
        printed = ' '.join(str(c) for c in mock_print.call_args_list)
        self.assertIn('direct HTTP', printed)


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
                    with patch('trustmux._enable.TOKENS_FILE') as tf:
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


if __name__ == '__main__':
    unittest.main()
