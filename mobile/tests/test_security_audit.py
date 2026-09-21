"""Regression tests for the 2026-09 security audit (trustmux side).

Each class names the finding it pins down.  Everything here runs against the
temp tree the other suites set up; nothing touches a real daemon or tmux.
"""
import hashlib
import base64
import json
import os
import re
import stat
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from tornado.testing import AsyncHTTPTestCase

import trustmux._daemon as bm
from trustmux import _paths


def _reset_pairing():
    bm._pair_code = ''
    bm._pair_attempts = 0
    bm._pair_attempts_by_ip.clear()
    bm._pair_code_mono_expiry = 0.0
    bm._pair_paired_ip = ''
    bm._sessions.clear()


class TestPairCrossSiteAndBudget(AsyncHTTPTestCase):
    """/pair: a page on another origin must not be able to spend the real
    phone's attempts, and one address must not be able to lock everyone out."""

    def get_app(self):
        return bm._make_app()

    def setUp(self):
        super().setUp()
        _reset_pairing()
        bm._pair_code = '424242'
        bm._pair_code_mono_expiry = time.monotonic() + 300

    def tearDown(self):
        _reset_pairing()
        super().tearDown()

    def _post(self, code='000000', **headers):
        h = {'Content-Type': 'application/json'}
        h.update(headers)
        return self.fetch('/pair', method='POST', body=json.dumps({'code': code}), headers=h)

    def test_form_content_types_are_refused_before_counting(self):
        for ctype in ('text/plain', 'application/x-www-form-urlencoded', 'multipart/form-data'):
            resp = self.fetch('/pair', method='POST', body='{"code":"000000"}',
                              headers={'Content-Type': ctype})
            # tornado itself rejects a boundary-less multipart body with 400;
            # either way it is refused before it can count as a guess.
            self.assertIn(resp.code, (400, 415), ctype)
        self.assertEqual(bm._pair_attempts, 0)

    def test_cross_site_fetch_is_refused_before_counting(self):
        resp = self._post(**{'Sec-Fetch-Site': 'cross-site'})
        self.assertEqual(resp.code, 403)
        resp = self._post(Origin='https://evil.example')
        self.assertEqual(resp.code, 403)
        self.assertEqual(bm._pair_attempts, 0)

    def test_same_origin_and_non_browser_requests_are_counted_normally(self):
        host = f'127.0.0.1:{self.get_http_port()}'
        resp = self._post(**{'Sec-Fetch-Site': 'same-origin', 'Origin': f'http://{host}'})
        self.assertEqual(resp.code, 403)          # wrong code, but a real guess
        self.assertEqual(bm._pair_attempts, 1)
        resp = self._post()                       # no browser headers at all
        self.assertEqual(bm._pair_attempts, 2)

    def test_attempts_are_counted_per_source_address(self):
        for _ in range(bm._MAX_PAIR_ATTEMPTS):
            self.assertEqual(self._post().code, 403)
        self.assertEqual(self._post().code, 429)
        # Another address still has its own budget while the total allows.
        self.assertLess(bm._pair_attempts, bm._MAX_PAIR_ATTEMPTS_TOTAL)
        self.assertEqual(bm._pair_attempts_by_ip.get('127.0.0.1'), bm._MAX_PAIR_ATTEMPTS)
        bm._pair_attempts_by_ip['10.0.0.9'] = 0
        # The guard for a fresh address is the total cap only.
        self.assertGreater(bm._MAX_PAIR_ATTEMPTS_TOTAL, bm._pair_attempts)

    def test_generating_a_code_resets_both_counters(self):
        bm._pair_attempts = 4
        bm._pair_attempts_by_ip['1.2.3.4'] = 3
        bm._generate_pair_code()
        self.assertEqual(bm._pair_attempts, 0)
        self.assertEqual(bm._pair_attempts_by_ip, {})

    def test_user_agent_is_sanitised_before_being_stored(self):
        code = bm._generate_pair_code()
        with patch('trustmux._daemon._save_tokens'):
            resp = self._post(code, **{'User-Agent': 'Evil\x1b[2J\x07Agent/1.0 \xe9'})
        self.assertEqual(resp.code, 200)
        label = next(iter(bm._sessions.values()))['label']
        self.assertEqual(label, 'Evil?[2J?Agent/1.0 ?')


class TestCsp(AsyncHTTPTestCase):
    """The inline theme bootstrap must be allowed by hash, never by
    'unsafe-inline', and the directives that do not inherit from default-src
    must be present."""

    def get_app(self):
        return bm._make_app()

    def test_inline_script_hash_matches_the_served_html(self):
        html = (bm.STATIC / 'index.html').read_text(encoding='utf-8')
        blocks = re.findall(r'<script>(.*?)</script>', html, re.S)
        self.assertTrue(blocks, 'expected at least one inline <script> in index.html')
        csp = self.fetch('/').headers.get('Content-Security-Policy', '')
        script_src = csp.split('script-src')[1].split(';')[0]
        for body in blocks:
            digest = base64.b64encode(hashlib.sha256(body.encode('utf-8')).digest()).decode()
            self.assertIn(f"'sha256-{digest}'", script_src)
        self.assertNotIn("'unsafe-inline'", script_src)

    def test_non_inheriting_directives_are_present(self):
        csp = self.fetch('/ping').headers.get('Content-Security-Policy', '')
        for directive in ("object-src 'none'", "base-uri 'none'",
                          "form-action 'self'", "frame-ancestors 'none'"):
            self.assertIn(directive, csp)


class TestCertReuse(unittest.TestCase):
    """The keypair survives restarts so a fingerprint can be pinned; only the
    certificate is reissued when the names change; the key is never
    world-readable, not even briefly."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        root = Path(self.td.name) / 'state'
        for attr, value in (('STATE_DIR', root), ('CERT_FILE', root / 'cert.pem'),
                            ('KEY_FILE', root / 'key.pem')):
            p = patch.object(bm, attr, value)
            p.start()
            self.addCleanup(p.stop)
        p = patch.object(bm, '_tailscale_ip', return_value=None)
        p.start()
        self.addCleanup(p.stop)

    def _gen(self, advertised=()):
        with patch('builtins.print'):
            bm._ensure_self_signed_cert('10.0.0.5', advertised)
        return bm.KEY_FILE.read_bytes(), bm.CERT_FILE.read_bytes(), bm._cert_fingerprint

    def test_key_and_cert_are_reused_when_names_are_covered(self):
        k1, c1, f1 = self._gen()
        k2, c2, f2 = self._gen()
        self.assertEqual(k1, k2)
        self.assertEqual(c1, c2)
        self.assertEqual(f1, f2)
        self.assertRegex(f1, r'^([0-9A-F]{2}:){31}[0-9A-F]{2}$')

    def test_new_name_reissues_cert_but_keeps_key(self):
        k1, c1, _ = self._gen()
        k2, c2, _ = self._gen(['tmux.example.com'])
        self.assertEqual(k1, k2)
        self.assertNotEqual(c1, c2)
        # And the reissued cert is then itself reused.
        k3, c3, _ = self._gen(['tmux.example.com'])
        self.assertEqual(c2, c3)

    def test_key_file_is_0600_and_state_dir_0700(self):
        self._gen()
        self.assertEqual(stat.S_IMODE(bm.KEY_FILE.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(bm.STATE_DIR.stat().st_mode), 0o700)

    def test_loose_state_dir_is_tightened_before_the_key_is_written(self):
        bm.STATE_DIR.mkdir(parents=True)
        bm.STATE_DIR.chmod(0o755)
        self._gen()
        self.assertEqual(stat.S_IMODE(bm.STATE_DIR.stat().st_mode), 0o700)

    def test_unreadable_existing_keypair_falls_back_to_a_fresh_one(self):
        bm.STATE_DIR.mkdir(parents=True)
        bm.KEY_FILE.write_text('garbage')
        bm.CERT_FILE.write_text('garbage')
        k, c, f = self._gen()
        self.assertIn(b'PRIVATE KEY', k)
        self.assertIn(b'CERTIFICATE', c)
        self.assertTrue(f)

    def test_write_private_never_passes_through_a_loose_mode(self):
        bm.STATE_DIR.mkdir(parents=True)
        target = bm.STATE_DIR / 'secret'
        old = os.umask(0o000)
        try:
            bm._write_private(target, b'x')
        finally:
            os.umask(old)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        self.assertFalse(target.with_suffix('.tmp').exists())


if __name__ == '__main__':
    unittest.main(verbosity=2)


# ---------------------------------------------------------------------------
# CLI side
# ---------------------------------------------------------------------------

import shutil
import subprocess
import trustmux._ctl as ctl
import trustmux._advertise as adv
import trustmux._enable as enable
import trustmux._disable as disable
import trustmux._unpair as unpair
from unittest.mock import call


class TestServeMappingLifecycle(unittest.TestCase):
    """The tailscale serve mapping must not outlive the daemon: while it does,
    it forwards the tailnet name to a loopback port any local user could bind."""

    def setUp(self):
        self.inst = ctl.Instance('servelife')
        self.inst.ensure_dirs()
        self.addCleanup(shutil.rmtree, self.inst.state, True)
        p = patch('trustmux._ctl.daemon_info', return_value=None)
        p.start(); self.addCleanup(p.stop)

    def test_serve_start_records_a_marker(self):
        with patch('trustmux._ctl._check_tmux', return_value=True), \
             patch('trustmux._ctl._check_tls', return_value=True), \
             patch('trustmux._ctl.subprocess.run'), \
             patch('trustmux._ctl._ts_host', return_value='h.ts.net'), \
             patch('trustmux._ctl._ensure_ts_serve', return_value=True), \
             patch('trustmux._ctl._launch', return_value=4242), \
             patch('trustmux._ctl.can_use_serve', return_value=True), \
             patch('builtins.print'):
            self.assertEqual(ctl.cmd_start('serve', 7432, self.inst), 0)
        self.assertEqual(self.inst.serve_marker.read_text().strip(), '7432')

    def test_stop_removes_the_mapping_and_the_marker(self):
        self.inst.serve_marker.write_text('7432\n')
        with patch('trustmux._paths.tailscale_cmd', return_value='tailscale'), \
             patch('trustmux._ctl._pid', return_value=None), \
             patch('trustmux._ctl.subprocess.run') as run, \
             patch('builtins.print'):
            self.assertEqual(ctl.cmd_stop(7432, self.inst), 0)
        self.assertIn(call(['tailscale', 'serve', '--bg', '7432', 'off'],
                           check=True, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=15),
                      run.call_args_list)
        self.assertFalse(self.inst.serve_marker.exists())

    def test_stop_keep_serve_leaves_it_and_says_what_that_means(self):
        self.inst.serve_marker.write_text('7432\n')
        with patch('trustmux._ctl._pid', return_value=None), \
             patch('trustmux._ctl.subprocess.run') as run, \
             patch('builtins.print') as mock_print:
            ctl.cmd_stop(7432, self.inst, keep_serve=True)
        run.assert_not_called()
        self.assertTrue(self.inst.serve_marker.exists())
        printed = ' '.join(str(c) for c in mock_print.call_args_list)
        self.assertIn('nothing listening', printed)

    def test_stop_without_a_marker_never_touches_tailscale(self):
        with patch('trustmux._ctl._pid', return_value=None), \
             patch('trustmux._ctl.subprocess.run',
                   side_effect=AssertionError('must not shell out')), \
             patch('builtins.print'):
            self.assertEqual(ctl.cmd_stop(7432, self.inst), 0)

    def test_failed_removal_keeps_the_marker_and_warns(self):
        self.inst.serve_marker.write_text('7432\n')
        with patch('trustmux._ctl._pid', return_value=None), \
             patch('trustmux._ctl.subprocess.run',
                   side_effect=subprocess.CalledProcessError(1, 'tailscale')), \
             patch('trustmux._ctl.subprocess.check_output',
                   return_value='https://h.ts.net -> http://127.0.0.1:7432'), \
             patch('builtins.print') as mock_print:
            ctl.cmd_stop(7432, self.inst)
        self.assertTrue(self.inst.serve_marker.exists())
        printed = ' '.join(str(c) for c in mock_print.call_args_list)
        self.assertIn('could not remove', printed)

    def test_status_warns_while_a_mapping_points_at_nothing(self):
        self.inst.serve_marker.write_text('7432\n')
        with patch('trustmux._ctl._pid', return_value=None), \
             patch('trustmux._ctl.subprocess.check_output',
                   return_value='https://h.ts.net -> http://127.0.0.1:7432'), \
             patch('builtins.print') as mock_print:
            self.assertEqual(ctl.cmd_status(7432, self.inst), 0)
        printed = ' '.join(str(c) for c in mock_print.call_args_list)
        self.assertIn('nothing is listening', printed)

    def test_status_forgets_a_marker_whose_mapping_is_already_gone(self):
        self.inst.serve_marker.write_text('7432\n')
        with patch('trustmux._ctl._pid', return_value=None), \
             patch('trustmux._ctl.subprocess.check_output', return_value=''), \
             patch('builtins.print'):
            ctl.cmd_status(7432, self.inst)
        self.assertFalse(self.inst.serve_marker.exists())

    def test_status_prints_the_certificate_fingerprint(self):
        fp = 'AA:BB:' * 15 + 'CC:DD'
        with patch('trustmux._ctl.daemon_info',
                   return_value={'pid': 1, 'port': 7432, 'scheme': 'https',
                                 'host': '0.0.0.0', 'advertise': [], 'fingerprint': fp}), \
             patch('trustmux._ctl._pid', return_value=1), \
             patch('builtins.print') as mock_print:
            ctl.cmd_status(7432, self.inst)
        printed = ' '.join(str(c) for c in mock_print.call_args_list)
        self.assertIn(fp, printed)


class TestAdvertiseConfigFileChecks(unittest.TestCase):
    """The instance config can name a program to run, so anything that lets
    someone else supply it is code execution: symlinks, foreign owners,
    writable parents."""

    def setUp(self):
        self.inst = ctl.Instance('advcfg')
        self.inst.config_file.parent.mkdir(parents=True, exist_ok=True)
        self.addCleanup(shutil.rmtree, _paths.config_dir(), True)

    def _write(self, mode=0o600):
        self.inst.config_file.write_text(json.dumps({'advertise': ['a.example.com']}))
        self.inst.config_file.chmod(mode)

    def test_own_0600_file_in_own_0700_dirs_is_read(self):
        self._write()
        self.assertEqual(adv.resolve_sources(None, False, self.inst), ['a.example.com'])

    def test_symlinked_config_is_refused(self):
        real = self.inst.config_file.with_name('real.json')
        real.write_text(json.dumps({'advertise': ['a.example.com']}))
        real.chmod(0o600)
        self.inst.config_file.symlink_to(real)
        with self.assertRaisesRegex(adv.AdvertiseError, 'symlink'):
            adv.resolve_sources(None, False, self.inst)

    def test_world_writable_parent_is_refused(self):
        self._write()
        self.inst.config_file.parent.chmod(0o777)
        self.addCleanup(self.inst.config_file.parent.chmod, 0o700)
        with self.assertRaisesRegex(adv.AdvertiseError, 'writable by group or other'):
            adv.resolve_sources(None, False, self.inst)

    def test_sticky_world_writable_parent_is_tolerated(self):
        self._write()
        self.inst.config_file.parent.chmod(0o1777)
        self.addCleanup(self.inst.config_file.parent.chmod, 0o700)
        self.assertEqual(adv.resolve_sources(None, False, self.inst), ['a.example.com'])

    def test_parent_writable_by_own_primary_group_is_tolerated(self):
        # Ubuntu user-private groups: umask 002 makes every dir 0775.
        self._write()
        d = self.inst.config_file.parent
        d.chmod(0o775)
        self.addCleanup(d.chmod, 0o700)
        if d.stat().st_gid != os.getgid():
            self.skipTest('temp dir not in primary group')
        self.assertEqual(adv.resolve_sources(None, False, self.inst), ['a.example.com'])

    def test_group_writable_file_is_still_refused(self):
        self._write(0o660)
        with self.assertRaisesRegex(adv.AdvertiseError, 'writable by group'):
            adv.resolve_sources(None, False, self.inst)


class TestInstanceNameStrictness(unittest.TestCase):
    def test_trailing_newline_is_rejected(self):
        with patch('builtins.print'), self.assertRaises(SystemExit):
            _paths.resolve_instance('work\n')

    def test_plain_name_still_accepted(self):
        self.assertEqual(_paths.resolve_instance('work').name, 'work')


class TestAtomicProfileRewrite(unittest.TestCase):
    def test_rewrite_keeps_mode_and_leaves_no_temp_file(self):
        with tempfile.TemporaryDirectory() as td:
            dest = Path(td) / '.profile'
            dest.write_text('a\nb\n')
            dest.chmod(0o640)
            enable.rewrite_in_place(dest, 'a\n')
            self.assertEqual(dest.read_text(), 'a\n')
            self.assertEqual(stat.S_IMODE(dest.stat().st_mode), 0o640)
            self.assertEqual(sorted(p.name for p in Path(td).iterdir()), ['.profile'])

    def test_disable_uses_it(self):
        with tempfile.TemporaryDirectory() as td:
            dest = Path(td) / '.profile'
            dest.write_text('keep\ntrustmux start 2>/dev/null || true\n')
            with patch('trustmux._disable.rewrite_in_place',
                       wraps=enable.rewrite_in_place) as rw:
                disable._remove_hook(dest, ctl.Instance())
            rw.assert_called_once()
            self.assertEqual(dest.read_text(), 'keep\n')


class TestUnpairLabelSanitised(unittest.TestCase):
    def test_control_bytes_are_replaced(self):
        self.assertEqual(unpair._ua_short('Evil\x1b[2Jthing'), 'Evil?[2Jthing')

    def test_known_browsers_still_shortened(self):
        self.assertEqual(unpair._ua_short('Mozilla/5.0 ... Mobile Safari'), 'Mobile')


class TestDaemonRefusesRoot(unittest.TestCase):
    def _main(self, euid, env):
        import sys
        with patch.object(sys, 'argv', ['trustmuxd', '--port', '7432']), \
             patch('trustmux._daemon.os.geteuid', return_value=euid), \
             patch.dict(os.environ, env, clear=False), \
             patch('trustmux._daemon.migrate_legacy_layout',
                   side_effect=RuntimeError('reached startup')), \
             patch('builtins.print'):
            return bm.main()

    def test_root_is_refused_before_touching_anything(self):
        os.environ.pop('TRUSTMUX_ALLOW_ROOT', None)
        with self.assertRaises(SystemExit) as cm:
            self._main(0, {})
        self.assertEqual(cm.exception.code, 1)

    def test_root_override_and_normal_user_proceed(self):
        with self.assertRaisesRegex(RuntimeError, 'reached startup'):
            self._main(0, {'TRUSTMUX_ALLOW_ROOT': '1'})
        os.environ.pop('TRUSTMUX_ALLOW_ROOT', None)
        with self.assertRaisesRegex(RuntimeError, 'reached startup'):
            self._main(1000, {})


# ---------------------------------------------------------------------------
# Serve mode over a Unix socket (follow-up to the port-squatting finding)
# ---------------------------------------------------------------------------

import asyncio
import signal
import tornado.httpserver
import tornado.netutil


class TestDaemonUnixListener(unittest.IsolatedAsyncioTestCase):
    """The HTTP listener can be a Unix socket in the state dir, 0600, so
    tailscale serve has nothing on loopback to proxy to and nobody else can
    bind in our place."""

    async def test_serves_http_over_a_0600_unix_socket(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, 'http.sock')
            server = tornado.httpserver.HTTPServer(bm._make_app(), xheaders=True)
            server.add_socket(tornado.netutil.bind_unix_socket(path, mode=0o600))
            try:
                self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
                reader, writer = await asyncio.open_unix_connection(path)
                writer.write(b'GET /ping HTTP/1.1\r\nHost: h.ts.net\r\n'
                             b'X-Forwarded-For: 100.64.0.9\r\nConnection: close\r\n\r\n')
                await writer.drain()
                raw = await asyncio.wait_for(reader.read(), 5)
                writer.close()
            finally:
                server.stop()
                await server.close_all_connections()
            head, _, body = raw.partition(b'\r\n\r\n')
            self.assertIn(b'HTTP/1.1 401', head)
            self.assertEqual(json.loads(body)['auth'], False)

    async def test_stale_socket_file_is_replaced(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, 'http.sock')
            old = tornado.netutil.bind_unix_socket(path)
            old.close()                       # daemon died; file left behind
            sock = tornado.netutil.bind_unix_socket(path, mode=0o600)
            sock.close()
            self.assertTrue(stat.S_ISSOCK(os.stat(path).st_mode))


class TestUnixServeCli(unittest.TestCase):
    def setUp(self):
        self.inst = ctl.Instance('unixserve')
        self.inst.ensure_dirs()
        self.addCleanup(shutil.rmtree, self.inst.state, True)
        p = patch('trustmux._ctl.daemon_info', return_value=None)
        p.start(); self.addCleanup(p.stop)

    def test_probe_reads_the_help_text(self):
        with patch('trustmux._ctl.subprocess.run',
                   return_value=subprocess.CompletedProcess([], 0,
                       stdout='... a Unix domain socket (e.g., unix:/tmp/x.sock).', stderr='')):
            self.assertTrue(ctl._ts_serve_supports_unix())
        with patch('trustmux._ctl.subprocess.run',
                   return_value=subprocess.CompletedProcess([], 0, stdout='old help', stderr='')):
            self.assertFalse(ctl._ts_serve_supports_unix())
        with patch('trustmux._ctl.subprocess.run', side_effect=FileNotFoundError):
            self.assertFalse(ctl._ts_serve_supports_unix())

    def _start(self, supports_unix, try_result=(True, False, ''), launch_pid=4242):
        """try_result: (ok, already_configured, stderr), what
        _try_ts_serve_target returns -- the function cmd_start actually
        calls now for the unix-socket attempt (and _ensure_ts_serve for
        the plain-port path, which wraps it and is left unmocked here so
        its own real "already configured" / status-check calls still run
        against the mocked subprocess.run below)."""
        with patch('trustmux._ctl._check_tmux', return_value=True), \
             patch('trustmux._ctl._check_tls', return_value=True), \
             patch('trustmux._ctl.subprocess.run'), \
             patch('trustmux._ctl.subprocess.check_output', side_effect=Exception), \
             patch('trustmux._ctl._ts_host', return_value='h.ts.net'), \
             patch('trustmux._ctl._ts_serve_supports_unix', return_value=supports_unix), \
             patch('trustmux._ctl._try_ts_serve_target', return_value=try_result) as try_target, \
             patch('trustmux._ctl._launch', return_value=launch_pid) as launch, \
             patch('trustmux._ctl.os.kill') as kill, \
             patch('trustmux._ctl.can_use_serve', return_value=True), \
             patch('builtins.print'):
            rc = ctl.cmd_start('serve', 7432, self.inst)
        return rc, try_target, launch, kill

    def test_unix_capable_tailscale_gets_a_socket_not_a_port(self):
        rc, try_target, launch, _ = self._start(True)
        self.assertEqual(rc, 0)
        args = launch.call_args.args[1]
        self.assertIn('--unix', args)
        self.assertIn(str(self.inst.http_sock), args)
        self.assertNotIn('--host', args)
        try_target.assert_called_once_with(f'unix:{self.inst.http_sock}')
        self.assertEqual(self.inst.serve_marker.read_text().strip(),
                         f'unix:{self.inst.http_sock}')

    def test_old_tailscale_falls_back_to_loopback_port(self):
        rc, try_target, launch, _ = self._start(False)
        self.assertEqual(rc, 0)
        args = launch.call_args.args[1]
        self.assertIn('--host', args)
        self.assertIn('127.0.0.1', args)
        # The plain-port path goes through _ensure_ts_serve ->
        # _ensure_ts_serve_target, which itself calls _try_ts_serve_target
        # (same mocked function) -- with a port target, not a unix: one.
        try_target.assert_called_once_with('7432')
        self.assertEqual(self.inst.serve_marker.read_text().strip(), '7432')

    def test_failed_mapping_stops_the_daemon_it_just_started(self):
        # A real error (not the sudo-for-unix-socket condition) must not
        # fall back silently -- it is reported and the just-started daemon
        # is torn back down.
        rc, _, _, kill = self._start(True, try_result=(False, False, 'some other error'))
        self.assertEqual(rc, 1)
        kill.assert_called_once_with(4242, signal.SIGTERM)
        self.assertFalse(self.inst.serve_marker.exists())

    def test_sudo_required_for_unix_socket_falls_back_to_port(self):
        # Confirmed directly against a real tailscaled (1.102.2): operator
        # permission alone -- already required, and already set up for
        # anyone who has used serve mode before -- is not enough to serve a
        # Unix socket; tailscale's own error names the extra requirement.
        # Falling back to a loopback port here, rather than failing, is
        # what keeps a setup that worked before Unix-socket mode existed
        # from breaking. The mocked target fails only for the unix:
        # attempt -- the fallback's own port-mode attempt must actually
        # succeed for this scenario to test what it claims to.
        err = ("sending serve config: 401 Unauthorized: must be root, or be "
               "an operator and able to run 'sudo tailscale' to serve a "
               "path or Unix socket")
        unix_target = f'unix:{self.inst.http_sock}'

        def fake_try(target):
            return (False, False, err) if target == unix_target else (True, False, '')

        with patch('trustmux._ctl._check_tmux', return_value=True), \
             patch('trustmux._ctl._check_tls', return_value=True), \
             patch('trustmux._ctl.subprocess.run'), \
             patch('trustmux._ctl.subprocess.check_output', side_effect=Exception), \
             patch('trustmux._ctl._ts_host', return_value='h.ts.net'), \
             patch('trustmux._ctl._ts_serve_supports_unix', return_value=True), \
             patch('trustmux._ctl._try_ts_serve_target', side_effect=fake_try) as try_target, \
             patch('trustmux._ctl._launch', return_value=4242) as launch, \
             patch('trustmux._ctl.os.kill') as kill, \
             patch('trustmux._ctl.can_use_serve', return_value=True), \
             patch('builtins.print'):
            rc = ctl.cmd_start('serve', 7432, self.inst)

        self.assertEqual(rc, 0)
        try_target.assert_any_call(unix_target)
        try_target.assert_any_call('7432')
        kill.assert_called_once_with(4242, signal.SIGTERM)
        args = launch.call_args_list[-1].args[1]
        self.assertIn('--host', args)
        self.assertIn('127.0.0.1', args)
        self.assertNotIn('--unix', args)
        self.assertEqual(self.inst.serve_marker.read_text().strip(), '7432')

    def test_stop_removes_a_unix_mapping(self):
        target = f'unix:{self.inst.http_sock}'
        self.inst.serve_marker.write_text(target + '\n')
        with patch('trustmux._paths.tailscale_cmd', return_value='tailscale'), \
             patch('trustmux._ctl._pid', return_value=None), \
             patch('trustmux._ctl.subprocess.run') as run, \
             patch('builtins.print'):
            self.assertEqual(ctl.cmd_stop(7432, self.inst), 0)
        self.assertEqual(run.call_args.args[0], ['tailscale', 'serve', '--bg', target, 'off'])
        self.assertFalse(self.inst.serve_marker.exists())

    def test_marker_parsing(self):
        self.inst.serve_marker.write_text('unix:/a/b/http.sock\n')
        self.assertEqual(ctl._serve_marker_target(self.inst), 'unix:/a/b/http.sock')
        self.inst.serve_marker.write_text('3389\n')
        self.assertEqual(ctl._serve_marker_target(self.inst), '3389')
        self.inst.serve_marker.write_text('garbage\n')
        self.assertIsNone(ctl._serve_marker_target(self.inst))
        self.assertEqual(ctl._serve_needle('unix:/x/y'), '/x/y')
        self.assertEqual(ctl._serve_needle('7432'), ':7432')
