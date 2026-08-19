"""Tests for Trustmux daemon — runs locally with stdlib unittest + tornado."""

import asyncio
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
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
        out = '%0\t0\t1\tbash\t1234\t0\tmain task\n%1\t1\t0\tvim\t5678\t1\t\n'
        panes = self._run(out)
        self.assertEqual(len(panes), 2)
        self.assertEqual(panes[0], {
            'id': '%0', 'index': 0, 'active': True,
            'command': 'bash', 'title': 'main task', 'dead': False,
        })
        self.assertEqual(panes[1], {
            'id': '%1', 'index': 1, 'active': False,
            'command': 'vim', 'title': '', 'dead': True,
        })

    def test_tab_in_title_does_not_shift_fields(self):
        out = '%0\t0\t1\tbash\t1234\t0\tfoo\tbar\n'
        panes = self._run(out)
        self.assertEqual(len(panes), 1)
        self.assertEqual(panes[0]['title'], 'foo\tbar')
        self.assertFalse(panes[0]['dead'])

    def test_empty_output(self):
        self.assertEqual(self._run(''), [])

    def test_skips_malformed_lines(self):
        out = 'garbage\n%0\t0\t1\tbash\t123\t0\ttitle\n'
        panes = self._run(out)
        self.assertEqual(len(panes), 1)
        self.assertEqual(panes[0]['id'], '%0')

    def test_active_flag_parsing(self):
        out = '%5\t0\t1\tzsh\t999\t0\ttitle\n'
        panes = self._run(out)
        self.assertTrue(panes[0]['active'])

        out = '%5\t0\t0\tzsh\t999\t0\ttitle\n'
        panes = self._run(out)
        self.assertFalse(panes[0]['active'])


class TestTmuxListWindows(unittest.TestCase):
    def test_parses_windows_with_panes(self):
        window_output = '@0\t0\tmain\t1\n@1\t1\twork\t0\n'
        pane_output   = '%0\t0\t1\tbash\t111\t0\ttitle\n'
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
        bm._pair_paired_ip = ''

    def tearDown(self):
        bm._pair_code = ''
        bm._pair_attempts = 0
        bm._pair_code_mono_expiry = 0.0
        bm._pair_paired_ip = ''
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
        # Outcome recorded for `trustmux pair` to report
        self.assertTrue(bm._pair_paired_ip)

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
# tmux_capture_pane join flag (-J: reflow soft-wrapped lines on the client)
# ---------------------------------------------------------------------------

class TestCapturePaneJoinFlag(unittest.TestCase):
    def test_join_true_passes_j_flag(self):
        captured_args = []
        def fake_tmux(*args):
            captured_args.extend(args)
            return ''
        with patch.object(bm, '_tmux', side_effect=fake_tmux):
            bm.tmux_capture_pane('%0', join=True)
        self.assertIn('-J', captured_args)

    def test_join_false_omits_j_flag(self):
        captured_args = []
        def fake_tmux(*args):
            captured_args.extend(args)
            return ''
        with patch.object(bm, '_tmux', side_effect=fake_tmux):
            bm.tmux_capture_pane('%0', join=False)
        self.assertNotIn('-J', captured_args)

    def test_default_omits_j_flag(self):
        captured_args = []
        def fake_tmux(*args):
            captured_args.extend(args)
            return ''
        with patch.object(bm, '_tmux', side_effect=fake_tmux):
            bm.tmux_capture_pane('%0')
        self.assertNotIn('-J', captured_args)


# ---------------------------------------------------------------------------
# tmux_cursor: cursor position mapped to a from-the-end capture line index
# ---------------------------------------------------------------------------

class TestTmuxCursor(unittest.TestCase):
    def test_maps_cursor_y_to_from_end_index(self):
        # Cursor on row 2 of a 10-row pane: 7 lines above the capture's last
        # line (a piped capture keeps trailing blank screen rows).
        with patch.object(bm, '_tmux', return_value='47\t2\t10\n'):
            self.assertEqual(bm.tmux_cursor('%0'),
                             {'cursor_x': 47, 'cursor_from_end': 7})

    def test_bottom_row_maps_to_zero(self):
        with patch.object(bm, '_tmux', return_value='0\t9\t10\n'):
            self.assertEqual(bm.tmux_cursor('%0')['cursor_from_end'], 0)

    def test_malformed_output_returns_none(self):
        for raw in ('', 'garbage', '1\t2', 'a\tb\tc', '0\t10\t10'):
            with patch.object(bm, '_tmux', return_value=raw):
                self.assertIsNone(bm.tmux_cursor('%0'), raw)


# ---------------------------------------------------------------------------
# _clamp_cursor: defense against a tmux that trims trailing blank rows
# ---------------------------------------------------------------------------

class TestClampCursor(unittest.TestCase):
    def test_cursor_within_capture_passes(self):
        cursor = {'cursor_x': 0, 'cursor_from_end': 2}
        self.assertEqual(bm._clamp_cursor(cursor, 'a\nb\nc'), cursor)

    def test_trailing_newline_closes_the_last_line(self):
        # 'a\nb\nc\n' is three lines, not four: from_end 2 is the first line.
        cursor = {'cursor_x': 0, 'cursor_from_end': 2}
        self.assertEqual(bm._clamp_cursor(cursor, 'a\nb\nc\n'), cursor)
        self.assertIsNone(
            bm._clamp_cursor({'cursor_x': 0, 'cursor_from_end': 3}, 'a\nb\nc\n'))

    def test_cursor_beyond_capture_drops(self):
        # A tmux that trims trailing blank screen rows would leave the
        # from-the-end index pointing into scrollback; drop the fields so
        # the client keeps its end-of-buffer anchor.
        self.assertIsNone(
            bm._clamp_cursor({'cursor_x': 0, 'cursor_from_end': 5}, 'a\nb'))

    def test_empty_capture_drops(self):
        self.assertIsNone(
            bm._clamp_cursor({'cursor_x': 0, 'cursor_from_end': 0}, ''))

    def test_none_stays_none(self):
        self.assertIsNone(bm._clamp_cursor(None, 'a\nb'))


# ---------------------------------------------------------------------------
# Pane stream: keystroke wake and burst coalescing
# ---------------------------------------------------------------------------

class _StreamPaneHarness(unittest.TestCase):
    """_stream_pane runs against a bare connection stand-in: it only touches
    _send, _stream_wake, and _last_input_mono, plus module globals patched
    here. Poll intervals are patched huge so only explicit wakes capture."""

    def _make_conn(self):
        conn = SimpleNamespace()
        conn._stream_wake = asyncio.Event()
        conn._last_input_mono = 0.0
        conn.sent = []
        conn._send = conn.sent.append
        return conn

    def _run_stream(self, conn, fake_capture, driver, settle, fake_cursor=None,
                    join=False, patch_mode=False):
        async def main():
            task = asyncio.ensure_future(
                bm.WsHandler._stream_pane(conn, '%0', 100, False, join,
                                          patch_mode))
            await asyncio.sleep(0.05)  # let the initial snapshot land
            result = await driver()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            return result
        async def no_session(pane_id):
            # Keep the stream on the subprocess fallback: a live control
            # client would capture the developer's real tmux instead of the
            # fakes patched below.
            return None
        with patch.object(bm, 'tmux_capture_pane', side_effect=fake_capture), \
             patch.object(bm, 'tmux_cursor',
                          side_effect=fake_cursor or (lambda pane_id: None)), \
             patch.object(bm._MONITOR, 'ensure', new=no_session), \
             patch.object(bm, '_POLL_IDLE', 60.0), \
             patch.object(bm, '_POLL_ACTIVE', 60.0), \
             patch.object(bm, '_KEYSTROKE_SETTLE', settle):
            return asyncio.run(main())


class TestControlClient(unittest.TestCase):
    """Reply-block parsing and %output wake dispatch of _ControlClient,
    driven through a fake proc whose stdout is a plain StreamReader."""

    def _fake_client(self):
        client = bm._ControlClient('$9')
        reader = asyncio.StreamReader()
        client.proc = SimpleNamespace(stdout=reader, returncode=None,
                                      terminate=lambda: None)
        return client, reader

    def test_reply_pairing_wake_error_and_eof(self):
        async def main():
            client, reader = self._fake_client()
            loop = asyncio.get_event_loop()
            first, second, third = (loop.create_future() for _ in range(3))
            client._pending.extend([first, second, third])
            woke = asyncio.Event()
            bm._MONITOR.watch('%9', woke)
            try:
                task = asyncio.ensure_future(client._read_loop())
                # Reply 1: an %end with mismatched ids is content, the
                # matching one closes the block.
                reader.feed_data(b'%begin 100 7 1\n'
                                 b'alpha\n'
                                 b'%end 999 8 1\n'
                                 b'%end 100 7 1\n')
                # A notification between blocks wakes the pane watcher.
                reader.feed_data(b'%output %9 abc\n')
                # Reply 2: %error resolves to "".
                reader.feed_data(b'%begin 101 8 1\n'
                                 b'no such pane\n'
                                 b'%error 101 8 1\n')
                self.assertEqual(await first, 'alpha\n%end 999 8 1\n')
                self.assertEqual(await second, '')
                await asyncio.wait_for(woke.wait(), 1)
                # EOF: the leftover pending future resolves to None and the
                # client reports itself gone.
                reader.feed_eof()
                self.assertIsNone(await third)
                await task
                self.assertFalse(client.alive)
            finally:
                bm._MONITOR.unwatch('%9', woke)
        asyncio.run(main())

    def test_wrong_pane_output_does_not_wake(self):
        async def main():
            client, reader = self._fake_client()
            woke = asyncio.Event()
            bm._MONITOR.watch('%9', woke)
            try:
                task = asyncio.ensure_future(client._read_loop())
                reader.feed_data(b'%output %8 abc\n')
                reader.feed_eof()
                await task
                self.assertFalse(woke.is_set())
            finally:
                bm._MONITOR.unwatch('%9', woke)
        asyncio.run(main())

    def test_cm_capture_falls_back_to_subprocess(self):
        async def main():
            async def no_client(session_id, cmd):
                return None
            with patch.object(bm._MONITOR, 'run', new=no_client), \
                 patch.object(bm, 'tmux_capture_pane',
                              return_value='fallback') as sub:
                out = await bm.cm_capture_pane('$1', '%0', 100, False, False)
            self.assertEqual(out, 'fallback')
            sub.assert_called_once_with('%0', 100, False, False)
        asyncio.run(main())

    def test_cm_capture_strips_ansi_like_subprocess_path(self):
        async def main():
            async def reply(session_id, cmd):
                return '\x1b[31mred\x1b[0m\n'
            with patch.object(bm._MONITOR, 'run', new=reply):
                plain = await bm.cm_capture_pane('$1', '%0', 100, False, False)
                ansi = await bm.cm_capture_pane('$1', '%0', 100, True, False)
            self.assertEqual(plain, 'red\n')
            self.assertEqual(ansi, '\x1b[31mred\x1b[0m\n')
        asyncio.run(main())


class TestStreamPaneWake(_StreamPaneHarness):
    def test_send_keys_wake_captures_before_poll_tick(self):
        conn = self._make_conn()
        captures = []
        def fake_capture(pane_id, lines, ansi, join):
            captures.append(time.monotonic())
            return f'content-{len(captures)}'

        async def driver():
            woke = time.monotonic()
            conn._last_input_mono = woke
            conn._stream_wake.set()
            await asyncio.sleep(0.1)
            return woke

        woke = self._run_stream(conn, fake_capture, driver, settle=0.01)
        # Snapshot plus exactly one wake-triggered capture, far inside the
        # 60s patched tick.
        self.assertEqual(len(captures), 2)
        self.assertLess(captures[1] - woke, 0.1)
        self.assertEqual(conn.sent[0]['type'], 'snapshot')
        self.assertEqual(conn.sent[1]['type'], 'update')
        self.assertEqual(conn.sent[1]['data'], 'content-2')

    def test_keystroke_burst_coalesces_into_one_capture(self):
        conn = self._make_conn()
        captures = []
        def fake_capture(pane_id, lines, ansi, join):
            captures.append(time.monotonic())
            return f'content-{len(captures)}'

        async def driver():
            # Three keystrokes inside one settle window (0.06s).
            for _ in range(3):
                conn._last_input_mono = time.monotonic()
                conn._stream_wake.set()
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.15)

        self._run_stream(conn, fake_capture, driver, settle=0.06)
        # Snapshot plus one coalesced capture, not one per keystroke.
        self.assertEqual(len(captures), 2)

    def test_unchanged_content_sends_no_update_on_wake(self):
        conn = self._make_conn()
        def fake_capture(pane_id, lines, ansi, join):
            return 'same'

        async def driver():
            conn._stream_wake.set()
            await asyncio.sleep(0.1)

        self._run_stream(conn, fake_capture, driver, settle=0.01)
        # Only the snapshot: the no-change suppression still holds on wakes.
        self.assertEqual(len(conn.sent), 1)
        self.assertEqual(conn.sent[0]['type'], 'snapshot')

    def test_cursor_fields_ride_snapshot_and_update(self):
        conn = self._make_conn()
        captures = []
        def fake_capture(pane_id, lines, ansi, join):
            captures.append(None)
            return f'a\nb\nc\nd\ncontent-{len(captures)}'
        def fake_cursor(pane_id):
            return {'cursor_x': 5, 'cursor_from_end': 3}

        async def driver():
            conn._stream_wake.set()
            await asyncio.sleep(0.1)

        self._run_stream(conn, fake_capture, driver, settle=0.01,
                         fake_cursor=fake_cursor)
        # A content change keeps the combined message: cursor fields ride
        # the full snapshot/update, never a separate cursor message.
        self.assertEqual([m['type'] for m in conn.sent],
                         ['snapshot', 'update'])
        for msg in conn.sent:
            self.assertEqual(msg['cursor_x'], 5)
            self.assertEqual(msg['cursor_from_end'], 3)

    def test_no_cursor_omits_the_fields(self):
        conn = self._make_conn()
        def fake_capture(pane_id, lines, ansi, join):
            return 'content'

        async def driver():
            return None

        self._run_stream(conn, fake_capture, driver, settle=0.01)
        self.assertEqual(conn.sent[0]['type'], 'snapshot')
        self.assertNotIn('cursor_x', conn.sent[0])
        self.assertNotIn('cursor_from_end', conn.sent[0])

    def test_cursor_move_alone_sends_a_cursor_message(self):
        # vi h/j/k/l moves the cursor without changing content; the client's
        # ghost anchor must follow without resending the unchanged capture,
        # so the daemon emits a data-less cursor message.
        conn = self._make_conn()
        cursors = []
        def fake_capture(pane_id, lines, ansi, join):
            return 'l1\nl2\nl3\nsame'
        def fake_cursor(pane_id):
            cursors.append(None)
            return {'cursor_x': 0, 'cursor_from_end': len(cursors)}

        async def driver():
            conn._stream_wake.set()
            await asyncio.sleep(0.1)

        self._run_stream(conn, fake_capture, driver, settle=0.01,
                         fake_cursor=fake_cursor)
        self.assertEqual(len(conn.sent), 2)
        self.assertEqual(conn.sent[1]['type'], 'cursor')
        self.assertEqual(conn.sent[1]['pane_id'], '%0')
        self.assertNotIn('data', conn.sent[1])
        self.assertEqual(conn.sent[1]['cursor_from_end'], 2)

    def test_unchanged_content_and_cursor_send_nothing(self):
        conn = self._make_conn()
        def fake_capture(pane_id, lines, ansi, join):
            return 'l1\nsame'
        def fake_cursor(pane_id):
            return {'cursor_x': 0, 'cursor_from_end': 1}

        async def driver():
            conn._stream_wake.set()
            await asyncio.sleep(0.1)

        self._run_stream(conn, fake_capture, driver, settle=0.01,
                         fake_cursor=fake_cursor)
        # Only the snapshot: no-change suppression covers the cursor too.
        self.assertEqual(len(conn.sent), 1)

    def test_cursor_going_unreadable_sends_one_fieldless_message(self):
        # The cursor read can start failing mid-stream (tmux hiccup): the
        # client must revert to its end-of-buffer anchor, so the daemon sends
        # one cursor message with the fields absent, then stays quiet while
        # the cursor remains unreadable.
        conn = self._make_conn()
        cursor_calls = []
        def fake_capture(pane_id, lines, ansi, join):
            return 'l1\nl2\nsame'
        def fake_cursor(pane_id):
            cursor_calls.append(None)
            if len(cursor_calls) == 1:
                return {'cursor_x': 0, 'cursor_from_end': 1}
            return None

        async def driver():
            conn._stream_wake.set()
            await asyncio.sleep(0.1)
            conn._stream_wake.set()
            await asyncio.sleep(0.1)

        self._run_stream(conn, fake_capture, driver, settle=0.01,
                         fake_cursor=fake_cursor)
        # Snapshot with cursor fields, then exactly one field-less cursor
        # message; the second wake repeats nothing (last_cursor advanced).
        self.assertEqual(len(conn.sent), 2)
        self.assertEqual(conn.sent[0]['type'], 'snapshot')
        self.assertEqual(conn.sent[0]['cursor_from_end'], 1)
        self.assertEqual(conn.sent[1], {'type': 'cursor', 'pane_id': '%0'})
        self.assertEqual(len(cursor_calls), 3)

    def test_join_subscribers_get_no_cursor(self):
        # A -J (wrap) capture merges soft-wrapped lines, so the from-the-end
        # mapping is unusable and the client falls back anyway; the daemon
        # never even reads the cursor.
        conn = self._make_conn()
        cursor_calls = []
        def fake_capture(pane_id, lines, ansi, join):
            return 'content'
        def fake_cursor(pane_id):
            cursor_calls.append(None)
            return {'cursor_x': 0, 'cursor_from_end': 0}

        async def driver():
            conn._stream_wake.set()
            await asyncio.sleep(0.1)

        self._run_stream(conn, fake_capture, driver, settle=0.01,
                         fake_cursor=fake_cursor, join=True)
        self.assertEqual(cursor_calls, [])
        self.assertNotIn('cursor_from_end', conn.sent[0])

    def test_cursor_beyond_capture_is_dropped_in_stream(self):
        # A trimming tmux (fewer capture lines than cursor_from_end + 1
        # implies) degrades to the client's end-of-buffer anchor: the
        # fields are dropped and no cursor message fires.
        conn = self._make_conn()
        def fake_capture(pane_id, lines, ansi, join):
            return 'one\ntwo'
        def fake_cursor(pane_id):
            return {'cursor_x': 0, 'cursor_from_end': 5}

        async def driver():
            conn._stream_wake.set()
            await asyncio.sleep(0.1)

        self._run_stream(conn, fake_capture, driver, settle=0.01,
                         fake_cursor=fake_cursor)
        self.assertEqual(len(conn.sent), 1)
        self.assertEqual(conn.sent[0]['type'], 'snapshot')
        self.assertNotIn('cursor_x', conn.sent[0])
        self.assertNotIn('cursor_from_end', conn.sent[0])


# ---------------------------------------------------------------------------
# _diff_line_ops: line-wise diff for patch subscribers
# ---------------------------------------------------------------------------

def _apply_ops(old, ops):
    """Reference client: apply ops in reverse so old-array indices stay valid."""
    new = list(old)
    for op in reversed(ops):
        new[op['start']:op['end']] = op.get('lines', [])
    return new


class TestDiffLineOps(unittest.TestCase):
    def test_equal_lists_yield_no_ops(self):
        self.assertEqual(bm._diff_line_ops(['a', 'b', ''], ['a', 'b', '']), [])

    def test_appended_line_is_one_insert(self):
        old = ['$ ls', '']
        new = ['$ ls', 'a.txt', '']
        ops = bm._diff_line_ops(old, new)
        # difflib anchors the insert before the trailing '' rather than
        # after it; both encodings rebuild the same list.
        self.assertEqual(ops, [{'op': 'insert', 'start': 1, 'end': 1,
                                'lines': ['a.txt']}])
        self.assertEqual(_apply_ops(old, ops), new)

    def test_blank_line_appended_after_tail_is_an_insert_at_old_length(self):
        # When the old trailing '' is absorbed into a longer equal block,
        # difflib anchors the insert after the old tail (start == end ==
        # len(old)). The client's applyPatch must then re-render the old
        # tail span with its newline: it stays mid-buffer otherwise.
        old = ['a', '']
        new = ['a', '', 'x', '']
        ops = bm._diff_line_ops(old, new)
        self.assertEqual(ops, [{'op': 'insert', 'start': 2, 'end': 2,
                                'lines': ['x', '']}])
        self.assertEqual(_apply_ops(old, ops), new)

    def test_replaced_line_carries_no_neighbors(self):
        old = ['a', 'b', 'c', '']
        new = ['a', 'B', 'c', '']
        ops = bm._diff_line_ops(old, new)
        self.assertEqual(ops, [{'op': 'replace', 'start': 1, 'end': 2,
                                'lines': ['B']}])
        self.assertEqual(_apply_ops(old, ops), new)

    def test_delete_op_has_no_lines(self):
        old = ['a', 'b', 'c', '']
        new = ['a', 'c', '']
        ops = bm._diff_line_ops(old, new)
        self.assertEqual(ops, [{'op': 'delete', 'start': 1, 'end': 2}])
        self.assertEqual(_apply_ops(old, ops), new)

    def test_scroll_is_a_delete_plus_insert_not_a_rewrite(self):
        # The steady-state case at full history: one line appended, every
        # line shifts up. The diff must ride the offset equal block, not
        # replace the whole buffer.
        old = [f'line-{i}' for i in range(50)] + ['']
        new = [f'line-{i}' for i in range(1, 51)] + ['']
        ops = bm._diff_line_ops(old, new)
        self.assertEqual(_apply_ops(old, ops), new)
        patched = sum(len(l) for op in ops for l in op.get('lines', []))
        self.assertLess(patched, len('\n'.join(new)) // 4)

    def test_repeated_blank_lines_do_not_degrade_the_diff(self):
        # Blank lines repeat far past SequenceMatcher's autojunk threshold;
        # with autojunk on they never match and a scroll turns into a
        # whole-buffer replace.
        old = [s for i in range(120) for s in (f'line-{i}', '')] + ['']
        new = old[2:-1] + ['line-120', '', '']
        ops = bm._diff_line_ops(old, new)
        self.assertEqual(_apply_ops(old, ops), new)
        patched = sum(len(l) for op in ops for l in op.get('lines', []))
        self.assertLess(patched, len('\n'.join(new)) // 4)


# ---------------------------------------------------------------------------
# Pane stream: patch opt-in (line-diff updates)
# ---------------------------------------------------------------------------

def _longline(i):
    # Realistic capture-line lengths: the fallback threshold compares the
    # encoded ops (which carry fixed per-op overhead) against the encoded
    # content, so toy two-character lines would always fall back to a full
    # update.
    return f'line-{i}-' + 'x' * 40


class TestStreamPanePatch(_StreamPaneHarness):
    def _run_two_captures(self, first, second, fake_cursor=None,
                          patch_mode=True):
        conn = self._make_conn()
        captures = []
        def fake_capture(pane_id, lines, ansi, join):
            captures.append(None)
            return first if len(captures) == 1 else second

        async def driver():
            conn._stream_wake.set()
            await asyncio.sleep(0.1)

        self._run_stream(conn, fake_capture, driver, settle=0.01,
                         fake_cursor=fake_cursor, patch_mode=patch_mode)
        return conn

    def test_opted_in_change_sends_patch_ops_after_full_snapshot(self):
        first  = f'{_longline(0)}\n{_longline(1)}\n'
        second = f'{first}{_longline(2)}\n'
        conn = self._run_two_captures(first, second)
        self.assertEqual([m['type'] for m in conn.sent], ['snapshot', 'patch'])
        self.assertEqual(conn.sent[0]['data'], first)
        msg = conn.sent[1]
        self.assertEqual(msg['pane_id'], '%0')
        self.assertNotIn('data', msg)
        self.assertEqual(msg['ops'], [{'op': 'insert', 'start': 2, 'end': 2,
                                       'lines': [_longline(2)]}])
        self.assertEqual(_apply_ops(first.split('\n'), msg['ops']),
                         second.split('\n'))

    def test_without_opt_in_the_full_update_is_unchanged(self):
        conn = self._run_two_captures('l1\nl2\n', 'l1\nl2\nl3\n',
                                      patch_mode=False)
        self.assertEqual(conn.sent[1],
                         {'type': 'update', 'pane_id': '%0',
                          'data': 'l1\nl2\nl3\n'})

    def test_whole_buffer_rewrite_falls_back_to_full_update(self):
        # Every line different: the encoded ops outweigh the content, so the
        # daemon sends a full update instead of a patch as large as one.
        conn = self._run_two_captures('aaaa\nbbbb\ncccc\n',
                                      'dddd\neeee\nffff\n')
        self.assertEqual(conn.sent[1],
                         {'type': 'update', 'pane_id': '%0',
                          'data': 'dddd\neeee\nffff\n'})

    def test_box_drawing_pane_still_patches_a_one_line_change(self):
        # Non-ASCII escapes to \uXXXX (6 bytes per char) under json.dumps;
        # the threshold compares encoded ops against encoded content, so the
        # escaping inflates both sides alike. Comparing encoded ops against
        # raw character count would tip this one-line change (one 40-char
        # line escaped to ~285 bytes vs 123 content characters) into a full
        # update.
        lines = ['│' + '─' * 38 + '│' for _ in range(3)]
        first = '\n'.join(lines) + '\n'
        changed = '│ ok' + '─' * 35 + '│'
        second = '\n'.join([lines[0], changed, lines[2]]) + '\n'
        conn = self._run_two_captures(first, second)
        msg = conn.sent[1]
        self.assertEqual(msg['type'], 'patch')
        self.assertEqual(_apply_ops(first.split('\n'), msg['ops']),
                         second.split('\n'))

    def test_cursor_fields_ride_the_patch(self):
        def fake_cursor(pane_id):
            return {'cursor_x': 5, 'cursor_from_end': 1}
        first  = f'{_longline(0)}\n{_longline(1)}\n'
        second = f'{first}{_longline(2)}\n'
        conn = self._run_two_captures(first, second,
                                      fake_cursor=fake_cursor)
        msg = conn.sent[1]
        self.assertEqual(msg['type'], 'patch')
        self.assertEqual(msg['cursor_x'], 5)
        self.assertEqual(msg['cursor_from_end'], 1)

    def test_cursor_move_alone_still_sends_a_cursor_message(self):
        conn = self._make_conn()
        cursors = []
        def fake_capture(pane_id, lines, ansi, join):
            return 'l1\nl2\nsame\n'
        def fake_cursor(pane_id):
            cursors.append(None)
            return {'cursor_x': 0, 'cursor_from_end': len(cursors) % 2}

        async def driver():
            conn._stream_wake.set()
            await asyncio.sleep(0.1)

        self._run_stream(conn, fake_capture, driver, settle=0.01,
                         fake_cursor=fake_cursor, patch_mode=True)
        self.assertEqual(conn.sent[1]['type'], 'cursor')
        self.assertNotIn('ops', conn.sent[1])

    def test_unchanged_content_sends_nothing(self):
        conn = self._run_two_captures('same\n', 'same\n')
        self.assertEqual([m['type'] for m in conn.sent], ['snapshot'])

    def test_wake_produces_a_patch_not_a_full_update(self):
        # The keystroke-wake path and the patch encoding compose: an echoed
        # keystroke arrives as one replaced prompt line, which is the
        # latency win this exists for.
        history = '\n'.join(_longline(i) for i in range(8))
        first  = f'{history}\n$ l\n'
        second = f'{history}\n$ ls\n'
        conn = self._run_two_captures(first, second)
        self.assertEqual(conn.sent[1]['type'], 'patch')
        self.assertEqual(conn.sent[1]['ops'],
                         [{'op': 'replace', 'start': 8, 'end': 9,
                           'lines': ['$ ls']}])
        self.assertEqual(_apply_ops(first.split('\n'), conn.sent[1]['ops']),
                         second.split('\n'))


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


class TestSelfSignedCertSans(unittest.TestCase):
    """An advertised host has to be in the certificate.

    A browser refuses a certificate that omits the name in the URL bar
    outright, rather than offering the click-through a self-signed one gets, so
    advertising an address without certifying it would be worse than useless.
    """

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        root = Path(self.td.name)
        for attr, value in (('STATE_DIR', root), ('CERT_FILE', root / 'cert.pem'),
                            ('KEY_FILE', root / 'key.pem')):
            p = patch.object(bm, attr, value)
            p.start()
            self.addCleanup(p.stop)
        # Keep the local-discovery SANs deterministic.
        p = patch.object(bm, '_tailscale_ip', return_value=None)
        p.start()
        self.addCleanup(p.stop)

    def _sans(self, lan_ip, advertised=()):
        from cryptography import x509
        from cryptography.x509.oid import ExtensionOID
        with patch('builtins.print'):
            bm._ensure_self_signed_cert(lan_ip, advertised)
        cert = x509.load_pem_x509_certificate(bm.CERT_FILE.read_bytes())
        ext = cert.extensions.get_extension_for_oid(
            ExtensionOID.SUBJECT_ALTERNATIVE_NAME).value
        return ([str(ip) for ip in ext.get_values_for_type(x509.IPAddress)],
                ext.get_values_for_type(x509.DNSName))

    def test_advertised_ip_becomes_an_ip_san(self):
        ips, _ = self._sans('10.128.0.7', ['34.1.2.3'])
        self.assertIn('34.1.2.3', ips)
        self.assertIn('10.128.0.7', ips)     # the local one is still there

    def test_advertised_name_becomes_a_dns_san(self):
        _, names = self._sans('10.128.0.7', ['tmux.example.com'])
        self.assertIn('tmux.example.com', names)

    def test_several_advertised_hosts_all_land(self):
        ips, names = self._sans('10.128.0.7', ['34.1.2.3', 'tmux.example.com'])
        self.assertIn('34.1.2.3', ips)
        self.assertIn('tmux.example.com', names)

    def test_advertised_ipv6_becomes_an_ip_san(self):
        ips, _ = self._sans('10.128.0.7', ['2001:db8::1'])
        self.assertIn('2001:db8::1', ips)

    def test_a_host_already_covered_is_not_duplicated(self):
        ips, _ = self._sans('10.128.0.7', ['10.128.0.7', '127.0.0.1'])
        self.assertEqual(ips.count('10.128.0.7'), 1)
        self.assertEqual(ips.count('127.0.0.1'), 1)

    def test_no_advertised_hosts_leaves_the_previous_sans_alone(self):
        ips, names = self._sans('10.128.0.7')
        self.assertIn('10.128.0.7', ips)
        self.assertIn('127.0.0.1', ips)
        self.assertIn('localhost', names)


if __name__ == '__main__':
    unittest.main(verbosity=2)
