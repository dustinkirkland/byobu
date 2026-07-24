"""Tests for Trustmux daemon — runs locally with stdlib unittest + tornado."""

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))
import trustmux._daemon as bm

from tornado.testing import AsyncHTTPTestCase


# ---------------------------------------------------------------------------
# ANSI stripping
# ---------------------------------------------------------------------------

class TestStripAnsi(unittest.TestCase):
    def test_removes_sgr_color(self):
        self.assertEqual(bm.strip_ansi('\x1b[31mred\x1b[0m'), 'red')

    def test_removes_bold(self):
        self.assertEqual(bm.strip_ansi('\x1b[1mbold\x1b[m'), 'bold')

    def test_removes_cursor_movement(self):
        self.assertEqual(bm.strip_ansi('\x1b[2J\x1b[H'), '')

    def test_removes_osc_window_title(self):
        self.assertEqual(bm.strip_ansi('\x1b]0;title\x07text'), 'text')

    def test_removes_carriage_return(self):
        self.assertEqual(bm.strip_ansi('hello\rworld'), 'helloworld')

    def test_passthrough_plain_text(self):
        self.assertEqual(bm.strip_ansi('hello world'), 'hello world')

    def test_passthrough_newlines(self):
        self.assertEqual(bm.strip_ansi('line1\nline2'), 'line1\nline2')

    def test_complex_prompt(self):
        # Typical byobu status chip: color + text + reset
        result = bm.strip_ansi('\x1b[48;5;24m\x1b[38;5;255m uptime \x1b[0m')
        self.assertEqual(result.strip(), 'uptime')


# ---------------------------------------------------------------------------
# tmux ID validation
# ---------------------------------------------------------------------------

class TestValidTmuxId(unittest.TestCase):
    def test_valid_session_ids(self):
        self.assertTrue(bm._valid_tmux_id('$0'))
        self.assertTrue(bm._valid_tmux_id('$123'))

    def test_valid_window_ids(self):
        self.assertTrue(bm._valid_tmux_id('@0'))
        self.assertTrue(bm._valid_tmux_id('@99'))

    def test_valid_pane_ids(self):
        self.assertTrue(bm._valid_tmux_id('%0'))
        self.assertTrue(bm._valid_tmux_id('%42'))

    def test_rejects_empty(self):
        self.assertFalse(bm._valid_tmux_id(''))

    def test_rejects_bare_digits(self):
        self.assertFalse(bm._valid_tmux_id('0'))
        self.assertFalse(bm._valid_tmux_id('123'))

    def test_rejects_wrong_sigil(self):
        self.assertFalse(bm._valid_tmux_id('!0'))
        self.assertFalse(bm._valid_tmux_id('#1'))

    def test_rejects_no_digits(self):
        self.assertFalse(bm._valid_tmux_id('$'))
        self.assertFalse(bm._valid_tmux_id('@'))
        self.assertFalse(bm._valid_tmux_id('%'))

    def test_rejects_alpha_suffix(self):
        self.assertFalse(bm._valid_tmux_id('$abc'))
        self.assertFalse(bm._valid_tmux_id('@1a'))


# ---------------------------------------------------------------------------
# tmux output parsing
# ---------------------------------------------------------------------------

class TestTmuxListPanes(unittest.TestCase):
    def _run(self, output):
        with patch.object(bm, '_tmux', return_value=output):
            return bm.tmux_list_panes('@0')

    def test_parses_two_panes(self):
        out = '%0\t0\t1\tbash\t1234\t0\n%1\t1\t0\tvim\t5678\t1\n'
        panes = self._run(out)
        self.assertEqual(len(panes), 2)
        self.assertEqual(panes[0], {'id': '%0', 'index': 0, 'active': True,  'command': 'bash', 'dead': False})
        self.assertEqual(panes[1], {'id': '%1', 'index': 1, 'active': False, 'command': 'vim',  'dead': True})

    def test_empty_output(self):
        self.assertEqual(self._run(''), [])

    def test_skips_malformed_lines(self):
        out = 'garbage\n%0\t0\t1\tbash\t123\n'
        panes = self._run(out)
        self.assertEqual(len(panes), 1)
        self.assertEqual(panes[0]['id'], '%0')

    def test_active_flag_parsing(self):
        out = '%5\t0\t1\tzsh\t999\n'
        panes = self._run(out)
        self.assertTrue(panes[0]['active'])

        out = '%5\t0\t0\tzsh\t999\n'
        panes = self._run(out)
        self.assertFalse(panes[0]['active'])


class TestTmuxListWindows(unittest.TestCase):
    def test_parses_windows_with_panes(self):
        window_output = '@0\t0\tmain\t1\n@1\t1\twork\t0\n'
        pane_output   = '%0\t0\t1\tbash\t111\n'
        call_count = 0

        def fake_tmux(*args):
            nonlocal call_count
            call_count += 1
            if 'list-windows' in args:
                return window_output
            return pane_output  # both window pane lists

        with patch.object(bm, '_tmux', side_effect=fake_tmux):
            windows = bm.tmux_list_windows('$0')

        self.assertEqual(len(windows), 2)
        self.assertEqual(windows[0]['id'], '@0')
        self.assertEqual(windows[0]['index'], 0)
        self.assertTrue(windows[0]['active'])
        self.assertFalse(windows[1]['active'])

    def test_empty_output(self):
        with patch.object(bm, '_tmux', return_value=''):
            windows = bm.tmux_list_windows('$0')
        self.assertEqual(windows, [])


# ---------------------------------------------------------------------------
# Byobu status config parsing
# ---------------------------------------------------------------------------

class TestReadByobuStatusConfig(unittest.TestCase):
    def test_defaults_when_no_config_exists(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(Path, 'home', return_value=Path(tmpdir)):
                left, right = bm._read_byobu_status_config()
        # Defaults should contain expected chips
        self.assertIn('session', left)
        self.assertIn('time', right)

    def test_parses_tmux_left_and_right(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg_dir = Path(tmpdir) / '.config' / 'byobu'
            cfg_dir.mkdir(parents=True)
            (cfg_dir / 'status').write_text(
                'tmux_left="logo session"\n'
                'tmux_right="uptime time"\n'
            )
            with patch.object(Path, 'home', return_value=Path(tmpdir)):
                left, right = bm._read_byobu_status_config()
        self.assertEqual(left, ['logo', 'session'])
        self.assertEqual(right, ['uptime', 'time'])

    def test_ignores_commented_lines(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg_dir = Path(tmpdir) / '.config' / 'byobu'
            cfg_dir.mkdir(parents=True)
            (cfg_dir / 'status').write_text(
                '#tmux_right="this should be ignored"\n'
                'tmux_left="session"\n'
            )
            with patch.object(Path, 'home', return_value=Path(tmpdir)):
                left, right = bm._read_byobu_status_config()
        self.assertEqual(left, ['session'])
        # right should be the default
        self.assertIn('time', right)

    def test_strips_quotes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg_dir = Path(tmpdir) / '.config' / 'byobu'
            cfg_dir.mkdir(parents=True)
            (cfg_dir / 'status').write_text('tmux_left="logo session uptime"\n')
            with patch.object(Path, 'home', return_value=Path(tmpdir)):
                left, _ = bm._read_byobu_status_config()
        self.assertEqual(left, ['logo', 'session', 'uptime'])


# ---------------------------------------------------------------------------
# Pair code generation
# ---------------------------------------------------------------------------

class TestPairCode(unittest.TestCase):
    def test_code_is_six_digits(self):
        code = bm._generate_pair_code()
        self.assertRegex(code, r'^\d{6}$')

    def test_code_sets_expiry(self):
        before = time.monotonic()
        bm._generate_pair_code()
        self.assertGreater(bm._pair_code_mono_expiry, before)

    def test_code_resets_attempts(self):
        bm._pair_attempts = 5
        bm._generate_pair_code()
        self.assertEqual(bm._pair_attempts, 0)

    def tearDown(self):
        bm._pair_code = ''
        bm._pair_code_expiry = 0.0
        bm._pair_code_mono_expiry = 0.0
        bm._pair_attempts = 0


# ---------------------------------------------------------------------------
# HTTP handler tests (Tornado test client — no network needed)
# ---------------------------------------------------------------------------

def _add_session(token='test_tok_abc'):
    bm._sessions[token] = {'ip': '127.0.0.1', 'paired_at': time.time(), 'label': 'test'}
    return token

def _clear_sessions():
    bm._sessions.clear()


class TestPingHandler(AsyncHTTPTestCase):
    def get_app(self):
        return bm._make_app()

    def setUp(self):
        super().setUp()
        _clear_sessions()
        bm._pair_code = ''

    def tearDown(self):
        _clear_sessions()
        super().tearDown()

    def test_unauthenticated_returns_401(self):
        resp = self.fetch('/ping')
        self.assertEqual(resp.code, 401)
        self.assertFalse(json.loads(resp.body)['auth'])

    def test_authenticated_via_cookie_returns_200(self):
        tok = _add_session()
        resp = self.fetch('/ping', headers={'Cookie': f'trustmux_session={tok}'})
        self.assertEqual(resp.code, 200)
        data = json.loads(resp.body)
        self.assertTrue(data['auth'])
        self.assertIn('hostname', data)

    def test_query_param_token_not_accepted(self):
        tok = _add_session()
        resp = self.fetch(f'/ping?token={tok}')
        self.assertEqual(resp.code, 401)

    def test_wrong_token_returns_401(self):
        _add_session('correct_token')
        resp = self.fetch('/ping', headers={'Cookie': 'trustmux_session=wrong_token'})
        self.assertEqual(resp.code, 401)


class TestPairHandler(AsyncHTTPTestCase):
    def get_app(self):
        return bm._make_app()

    def setUp(self):
        super().setUp()
        _clear_sessions()
        bm._pair_code = ''
        bm._pair_attempts = 0
        bm._pair_code_mono_expiry = 0.0

    def tearDown(self):
        bm._pair_code = ''
        bm._pair_attempts = 0
        bm._pair_code_mono_expiry = 0.0
        _clear_sessions()
        super().tearDown()

    def _post(self, body):
        return self.fetch('/pair', method='POST',
                          body=json.dumps(body),
                          headers={'Content-Type': 'application/json'})

    def test_no_active_code_returns_403(self):
        resp = self._post({'code': '123456'})
        self.assertEqual(resp.code, 403)

    def test_expired_code_returns_403(self):
        bm._pair_code = '123456'
        bm._pair_code_mono_expiry = time.monotonic() - 1  # already expired
        resp = self._post({'code': '123456'})
        self.assertEqual(resp.code, 403)
        self.assertEqual(bm._pair_code, '')  # code cleared

    def test_wrong_code_returns_403_and_increments_attempts(self):
        bm._pair_code = '999999'
        bm._pair_code_mono_expiry = time.monotonic() + 300
        resp = self._post({'code': '000000'})
        self.assertEqual(resp.code, 403)
        self.assertEqual(bm._pair_attempts, 1)

    def test_valid_code_returns_200_and_sets_cookie(self):
        code = bm._generate_pair_code()
        with patch('trustmux._daemon._save_tokens'):
            resp = self._post({'code': code})
        self.assertEqual(resp.code, 200)
        self.assertTrue(json.loads(resp.body).get('ok'))
        self.assertIn('trustmux_session', resp.headers.get('Set-Cookie', ''))
        # Code consumed — one-time use
        self.assertEqual(bm._pair_code, '')

    def test_valid_code_with_dashes(self):
        code = bm._generate_pair_code()
        dashed = f'{code[:3]}-{code[3:]}'
        with patch('trustmux._daemon._save_tokens'):
            resp = self._post({'code': dashed})
        self.assertEqual(resp.code, 200)

    def test_too_many_attempts_returns_429(self):
        bm._pair_code = '111111'
        bm._pair_code_mono_expiry = time.monotonic() + 300
        bm._pair_attempts = bm._MAX_PAIR_ATTEMPTS
        resp = self._post({'code': '111111'})
        self.assertEqual(resp.code, 429)

    def test_invalid_json_returns_400(self):
        bm._pair_code = '123456'
        bm._pair_code_mono_expiry = time.monotonic() + 300
        resp = self.fetch('/pair', method='POST', body='not-json',
                          headers={'Content-Type': 'application/json'})
        self.assertEqual(resp.code, 400)

    def test_request_too_large_returns_413(self):
        bm._pair_code = '123456'
        bm._pair_code_mono_expiry = time.monotonic() + 300
        resp = self.fetch('/pair', method='POST',
                          body='x' * 2000,
                          headers={'Content-Type': 'application/json'})
        self.assertEqual(resp.code, 413)


class TestManifestHandler(AsyncHTTPTestCase):
    def get_app(self):
        return bm._make_app()

    def test_manifest_contains_hostname(self):
        import socket
        resp = self.fetch('/manifest.json')
        self.assertEqual(resp.code, 200)
        data = json.loads(resp.body)
        hostname = socket.gethostname().split('.')[0]
        self.assertIn(hostname, data['name'])
        self.assertEqual(data['short_name'], hostname)

    def test_manifest_has_required_fields(self):
        resp = self.fetch('/manifest.json')
        data = json.loads(resp.body)
        for field in ('name', 'short_name', 'start_url', 'display', 'icons'):
            self.assertIn(field, data, f'missing field: {field}')

    def test_manifest_no_cache(self):
        resp = self.fetch('/manifest.json')
        self.assertIn('no-cache', resp.headers.get('Cache-Control', ''))


class TestStatusHandler(AsyncHTTPTestCase):
    def get_app(self):
        return bm._make_app()

    def setUp(self):
        super().setUp()
        _clear_sessions()

    def tearDown(self):
        _clear_sessions()
        super().tearDown()

    def test_unauthenticated_returns_401(self):
        resp = self.fetch('/status')
        self.assertEqual(resp.code, 401)

    def test_authenticated_returns_dict_with_left_right(self):
        tok = _add_session()
        with patch('trustmux._daemon.read_byobu_status', return_value={'left': [], 'right': []}):
            resp = self.fetch('/status', headers={'Cookie': f'trustmux_session={tok}'})
        self.assertEqual(resp.code, 200)
        data = json.loads(resp.body)
        self.assertIn('left', data)
        self.assertIn('right', data)


# ---------------------------------------------------------------------------
# tmux_capture_pane ANSI flag
# ---------------------------------------------------------------------------

class TestCapturePaneAnsiFlag(unittest.TestCase):
    def test_plain_strips_ansi(self):
        colored = '\x1b[31mhello\x1b[0m'
        with patch.object(bm, '_tmux', return_value=colored):
            result = bm.tmux_capture_pane('%0', ansi=False)
        self.assertEqual(result, 'hello')

    def test_ansi_true_passes_through(self):
        colored = '\x1b[31mhello\x1b[0m'
        with patch.object(bm, '_tmux', return_value=colored):
            result = bm.tmux_capture_pane('%0', ansi=True)
        self.assertEqual(result, colored)

    def test_ansi_true_passes_e_flag(self):
        captured_args = []
        def fake_tmux(*args):
            captured_args.extend(args)
            return ''
        with patch.object(bm, '_tmux', side_effect=fake_tmux):
            bm.tmux_capture_pane('%0', ansi=True)
        self.assertIn('-e', captured_args)

    def test_ansi_false_omits_e_flag(self):
        captured_args = []
        def fake_tmux(*args):
            captured_args.extend(args)
            return ''
        with patch.object(bm, '_tmux', side_effect=fake_tmux):
            bm.tmux_capture_pane('%0', ansi=False)
        self.assertNotIn('-e', captured_args)


# ---------------------------------------------------------------------------
# main() -- help discoverability
# ---------------------------------------------------------------------------

class TestMainHelp(unittest.TestCase):
    """`trustmuxd help` is a natural guess (matches `trustmux help`) but
    argparse has no subcommands here to hang a hidden alias off of --
    without the intercept it hits "unrecognized arguments: help" instead
    of the actual help text."""

    def test_help_arg_prints_full_help_and_exits_zero(self):
        with patch.object(sys, 'argv', ['trustmuxd', 'help']), \
             patch.object(bm.argparse.ArgumentParser, 'print_help') as mock_print:
            with self.assertRaises(SystemExit) as cm:
                bm.main()
            mock_print.assert_called_once()
            self.assertEqual(cm.exception.code, 0)


if __name__ == '__main__':
    unittest.main(verbosity=2)
