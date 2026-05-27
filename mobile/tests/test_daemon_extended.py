"""Extended test suite for Trustmux daemon.

Covers: token persistence, security headers, static handlers, MachinesHandler,
tmux list/write ops, byobu status chips, admin socket protocol, WebSocket handler.
"""

import asyncio
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock

sys.path.insert(0, str(Path(__file__).parent.parent))
import trustmux as bm

from tornado.testing import AsyncHTTPTestCase, gen_test
from tornado.websocket import websocket_connect


# ---------------------------------------------------------------------------
# Helpers shared across test classes
# ---------------------------------------------------------------------------

def _add_session(token='tok_ext_test'):
    bm._sessions[token] = {
        'ip': '127.0.0.1',
        'paired_at': time.time(),
        'label': 'test-agent/1.0',
    }
    return token


def _clear_sessions():
    bm._sessions.clear()


# ---------------------------------------------------------------------------
# Token persistence
# ---------------------------------------------------------------------------

class TestTokenPersistence(unittest.TestCase):
    def setUp(self):
        _clear_sessions()
        self._tmpdir = tempfile.TemporaryDirectory()
        self._orig_config = bm.CONFIG_DIR
        self._orig_tokens = bm.TOKENS_FILE
        bm.CONFIG_DIR = Path(self._tmpdir.name) / 'trustmux'
        bm.TOKENS_FILE = bm.CONFIG_DIR / 'tokens.json'

    def tearDown(self):
        _clear_sessions()
        bm.CONFIG_DIR = self._orig_config
        bm.TOKENS_FILE = self._orig_tokens
        self._tmpdir.cleanup()

    def _write_tokens(self, data):
        bm.CONFIG_DIR.mkdir(parents=True)
        bm.TOKENS_FILE.write_text(json.dumps(data))

    def test_load_missing_file_is_no_op(self):
        bm._load_tokens()
        self.assertEqual(bm._sessions, {})

    def test_load_valid_tokens(self):
        self._write_tokens({'mytoken': {'ip': '1.2.3.4', 'paired_at': 1234.0, 'label': 'x'}})
        bm._load_tokens()
        self.assertIn('mytoken', bm._sessions)
        self.assertEqual(bm._sessions['mytoken']['ip'], '1.2.3.4')

    def test_load_skips_malformed_records(self):
        self._write_tokens({
            'good': {'ip': '1.1.1.1', 'paired_at': 1.0, 'label': ''},
            'bad_str': 'not-a-dict',
            'bad_missing': {'label': 'no ip or paired_at'},
        })
        bm._load_tokens()
        self.assertIn('good', bm._sessions)
        self.assertNotIn('bad_str', bm._sessions)
        self.assertNotIn('bad_missing', bm._sessions)

    def test_load_corrupt_json_does_not_raise(self):
        bm.CONFIG_DIR.mkdir(parents=True)
        bm.TOKENS_FILE.write_text('not json!!!')
        bm._load_tokens()
        self.assertEqual(bm._sessions, {})

    def test_load_wrong_root_type_ignored(self):
        self._write_tokens([1, 2, 3])
        bm._load_tokens()
        self.assertEqual(bm._sessions, {})

    def test_save_creates_tokens_file(self):
        bm._sessions['tok123'] = {'ip': '9.9.9.9', 'paired_at': 1.0, 'label': 'y'}
        bm._save_tokens()
        self.assertTrue(bm.TOKENS_FILE.exists())
        data = json.loads(bm.TOKENS_FILE.read_text())
        self.assertIn('tok123', data)
        self.assertEqual(data['tok123']['ip'], '9.9.9.9')

    def test_save_file_mode_600(self):
        bm._sessions['tok456'] = {'ip': '1.1.1.1', 'paired_at': 2.0, 'label': ''}
        bm._save_tokens()
        mode = oct(bm.TOKENS_FILE.stat().st_mode)[-3:]
        self.assertEqual(mode, '600')

    def test_save_and_reload_roundtrip(self):
        bm._sessions['roundtrip'] = {'ip': '5.5.5.5', 'paired_at': 99.0, 'label': 'rt'}
        bm._save_tokens()
        _clear_sessions()
        bm._load_tokens()
        self.assertIn('roundtrip', bm._sessions)

    def test_valid_session_token_true_for_known(self):
        bm._sessions['abc123'] = {'ip': '1.1.1.1', 'paired_at': 1.0, 'label': ''}
        self.assertTrue(bm._valid_session_token('abc123'))

    def test_valid_session_token_false_for_unknown(self):
        self.assertFalse(bm._valid_session_token('nosuchtoken'))

    def test_valid_session_token_false_for_empty(self):
        self.assertFalse(bm._valid_session_token(''))


# ---------------------------------------------------------------------------
# Security headers — present on all endpoints
# ---------------------------------------------------------------------------

class TestSecurityHeaders(AsyncHTTPTestCase):
    def get_app(self):
        return bm._make_app()

    def setUp(self):
        super().setUp()
        _clear_sessions()

    def tearDown(self):
        _clear_sessions()
        super().tearDown()

    def _assert_security_headers(self, resp):
        h = resp.headers
        self.assertIn('DENY', h.get('X-Frame-Options', ''), 'X-Frame-Options missing')
        self.assertIn('default-src', h.get('Content-Security-Policy', ''), 'CSP missing')
        self.assertIn('no-referrer', h.get('Referrer-Policy', ''), 'Referrer-Policy missing')
        self.assertIn('nosniff', h.get('X-Content-Type-Options', ''), 'X-Content-Type-Options missing')

    def test_ping_has_security_headers(self):
        self._assert_security_headers(self.fetch('/ping'))

    def test_manifest_has_security_headers(self):
        self._assert_security_headers(self.fetch('/manifest.json'))

    def test_status_has_security_headers(self):
        self._assert_security_headers(self.fetch('/status'))

    def test_pair_post_has_security_headers(self):
        bm._pair_code = '123456'
        bm._pair_code_mono_expiry = time.monotonic() + 300
        resp = self.fetch('/pair', method='POST', body='{}',
                          headers={'Content-Type': 'application/json'})
        self._assert_security_headers(resp)
        bm._pair_code = ''


# ---------------------------------------------------------------------------
# Static file handlers
# ---------------------------------------------------------------------------

class TestStaticHandlers(AsyncHTTPTestCase):
    def get_app(self):
        return bm._make_app()

    def test_index_content_type_html(self):
        resp = self.fetch('/')
        self.assertEqual(resp.code, 200)
        self.assertIn('text/html', resp.headers.get('Content-Type', ''))

    def test_service_worker_no_cache(self):
        resp = self.fetch('/sw.js')
        self.assertEqual(resp.code, 200)
        self.assertIn('no-cache', resp.headers.get('Cache-Control', ''))

    def test_service_worker_content_type_js(self):
        resp = self.fetch('/sw.js')
        self.assertIn('javascript', resp.headers.get('Content-Type', ''))

    def test_svg_content_type(self):
        resp = self.fetch('/trustmux.svg')
        self.assertEqual(resp.code, 200)
        self.assertIn('svg', resp.headers.get('Content-Type', ''))

    def test_svg_has_cache_header(self):
        resp = self.fetch('/trustmux.svg')
        self.assertIn('max-age', resp.headers.get('Cache-Control', ''))

    def test_icon_invalid_name_returns_404(self):
        resp = self.fetch('/icons/nonexistent_icon_99999.png')
        self.assertEqual(resp.code, 404)

    def test_icon_path_traversal_blocked(self):
        resp = self.fetch('/icons/../trustmux.svg')
        # Tornado routing won't match this; should 404
        self.assertIn(resp.code, (404, 400))

    def test_icon_name_with_illegal_chars_returns_404(self):
        resp = self.fetch('/icons/foo;bar.png')
        self.assertEqual(resp.code, 404)

    def test_icon_existing_returns_200(self):
        resp = self.fetch('/icons/icon-192.png')
        self.assertEqual(resp.code, 200)
        self.assertEqual(resp.headers.get('Content-Type'), 'image/png')

    def test_icon_cache_header(self):
        resp = self.fetch('/icons/icon-192.png')
        self.assertIn('max-age', resp.headers.get('Cache-Control', ''))


# ---------------------------------------------------------------------------
# MachinesHandler
# ---------------------------------------------------------------------------

class TestMachinesHandler(AsyncHTTPTestCase):
    def get_app(self):
        return bm._make_app()

    def setUp(self):
        super().setUp()
        self._tmpdir = tempfile.TemporaryDirectory()
        self._orig = bm.MACHINES_FILE
        bm.MACHINES_FILE = Path(self._tmpdir.name) / 'machines.json'

    def tearDown(self):
        bm.MACHINES_FILE = self._orig
        self._tmpdir.cleanup()
        super().tearDown()

    def test_no_file_returns_only_current(self):
        resp = self.fetch('/machines')
        self.assertEqual(resp.code, 200)
        data = json.loads(resp.body)
        self.assertEqual(len(data), 1)
        self.assertTrue(data[0]['current'])

    def test_with_siblings_returns_all(self):
        siblings = [
            {'name': 'work', 'url': 'https://work.ts.net'},
            {'name': 'home', 'url': 'https://home.ts.net'},
        ]
        bm.MACHINES_FILE.write_text(json.dumps(siblings))
        resp = self.fetch('/machines')
        data = json.loads(resp.body)
        self.assertEqual(len(data), 3)
        urls = [m['url'] for m in data]
        self.assertIn('https://work.ts.net', urls)

    def test_malformed_entry_skipped(self):
        siblings = [
            {'name': 'good', 'url': 'https://good.ts.net'},
            {'missing_url': True},
        ]
        bm.MACHINES_FILE.write_text(json.dumps(siblings))
        resp = self.fetch('/machines')
        data = json.loads(resp.body)
        # Only current + 'good'; malformed entry absent
        self.assertEqual(len(data), 2)

    def test_non_list_json_returns_current_only(self):
        bm.MACHINES_FILE.write_text(json.dumps({'not': 'a list'}))
        resp = self.fetch('/machines')
        data = json.loads(resp.body)
        self.assertEqual(len(data), 1)

    def test_corrupt_json_returns_500(self):
        bm.MACHINES_FILE.write_text('not valid json!!!')
        resp = self.fetch('/machines')
        self.assertEqual(resp.code, 500)


# ---------------------------------------------------------------------------
# tmux_list_sessions parsing
# ---------------------------------------------------------------------------

class TestTmuxListSessions(unittest.TestCase):
    def _run(self, sess_output):
        def fake_tmux(*args):
            if 'list-sessions' in args:
                return sess_output
            if 'list-windows' in args:
                return '@0\t0\tmain\t1\n'
            return '%0\t0\t1\tbash\t123\n'
        with patch.object(bm, '_tmux', side_effect=fake_tmux):
            return bm.tmux_list_sessions()

    def test_parses_attached_session(self):
        sessions = self._run('$0\tmysession\t1\n')
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]['id'], '$0')
        self.assertEqual(sessions[0]['name'], 'mysession')
        self.assertTrue(sessions[0]['attached'])

    def test_parses_detached_session(self):
        sessions = self._run('$1\twork\t0\n')
        self.assertFalse(sessions[0]['attached'])

    def test_multiple_sessions(self):
        sessions = self._run('$0\ta\t1\n$1\tb\t0\n')
        self.assertEqual(len(sessions), 2)

    def test_empty_output(self):
        self.assertEqual(self._run(''), [])

    def test_session_has_windows_list(self):
        sessions = self._run('$0\tmain\t1\n')
        self.assertIn('windows', sessions[0])
        self.assertIsInstance(sessions[0]['windows'], list)


# ---------------------------------------------------------------------------
# tmux write operations
# ---------------------------------------------------------------------------

class TestTmuxWriteOps(unittest.TestCase):
    def _capture(self, fn, *args, **kwargs):
        calls = []
        with patch.object(bm, '_tmux', side_effect=lambda *a: calls.append(a) or ''):
            fn(*args, **kwargs)
        return calls

    def test_new_session_args(self):
        calls = self._capture(bm.tmux_new_session, 'mysession')
        self.assertEqual(len(calls), 1)
        self.assertIn('new-session', calls[0])
        self.assertIn('-d', calls[0])
        self.assertIn('-s', calls[0])
        self.assertIn('mysession', calls[0])

    def test_new_window_with_name(self):
        calls = self._capture(bm.tmux_new_window, '$0', 'vim')
        self.assertIn('new-window', calls[0])
        self.assertIn('-n', calls[0])
        self.assertIn('vim', calls[0])

    def test_new_window_without_name_omits_n_flag(self):
        calls = self._capture(bm.tmux_new_window, '$0', '')
        self.assertIn('new-window', calls[0])
        self.assertNotIn('-n', calls[0])

    def test_new_pane_calls_split_window(self):
        calls = self._capture(bm.tmux_new_pane, '@1')
        self.assertIn('split-window', calls[0])
        self.assertIn('@1', calls[0])

    def test_send_keys_with_enter_makes_two_calls(self):
        calls = self._capture(bm.tmux_send_keys, '%0', 'ls', True)
        self.assertEqual(len(calls), 2)
        self.assertIn('ls', calls[0])
        self.assertIn('Enter', calls[1])

    def test_send_keys_without_enter_makes_one_call(self):
        calls = self._capture(bm.tmux_send_keys, '%0', 'ls', False)
        self.assertEqual(len(calls), 1)
        self.assertIn('ls', calls[0])

    def test_send_keys_uses_literal_flag(self):
        calls = self._capture(bm.tmux_send_keys, '%0', 'my text', False)
        self.assertIn('-l', calls[0])


# ---------------------------------------------------------------------------
# Byobu status — _first_attr
# ---------------------------------------------------------------------------

class TestFirstAttr(unittest.TestCase):
    def test_extracts_bg(self):
        self.assertEqual(bm._first_attr('#[bg=blue,fg=white]text', 'bg='), 'blue')

    def test_extracts_fg(self):
        self.assertEqual(bm._first_attr('#[bg=black,fg=brightwhite]text', 'fg='), 'brightwhite')

    def test_no_match_returns_none(self):
        self.assertIsNone(bm._first_attr('plain text', 'bg='))

    def test_empty_attr_value_returns_none(self):
        self.assertIsNone(bm._first_attr('#[bg=]text', 'bg='))

    def test_first_block_wins(self):
        self.assertEqual(bm._first_attr('#[bg=red]#[bg=blue]', 'bg='), 'red')

    def test_no_attr_blocks_returns_none(self):
        self.assertIsNone(bm._first_attr('', 'bg='))


# ---------------------------------------------------------------------------
# Byobu status — _make_chip
# ---------------------------------------------------------------------------

class TestMakeChip(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._shm = Path(self._tmpdir.name)
        self._status_dir = self._shm / 'status.tmux'
        self._status_dir.mkdir()

    def tearDown(self):
        self._tmpdir.cleanup()

    def _write_chip(self, name, content):
        (self._status_dir / name).write_text(content)

    def test_logo_returns_none(self):
        self.assertIsNone(bm._make_chip('logo', self._shm))

    def test_missing_status_dir_returns_none(self):
        shm2 = Path(self._tmpdir.name + '_2')
        shm2.mkdir()
        self.assertIsNone(bm._make_chip('uptime', shm2))

    def test_missing_chip_file_returns_none(self):
        self.assertIsNone(bm._make_chip('nonexistent', self._shm))

    def test_all_whitespace_chip_returns_none(self):
        self._write_chip('blank', '#[bg=blue]   #[fg=white]')
        self.assertIsNone(bm._make_chip('blank', self._shm))

    def test_valid_chip_returns_dict(self):
        self._write_chip('uptime', '#[bg=blue,fg=white] 5 days #[default]')
        chip = bm._make_chip('uptime', self._shm)
        self.assertIsNotNone(chip)
        self.assertEqual(chip['label'], 'uptime')
        self.assertIn('5', chip['text'])
        self.assertIn('bg', chip)
        self.assertIn('color', chip)
        self.assertIn('text', chip)

    def test_named_color_maps_to_css_hex(self):
        self._write_chip('time', '#[bg=cyan] 12:00 ')
        chip = bm._make_chip('time', self._shm)
        self.assertEqual(chip['bg'], bm._BG['cyan'])

    def test_css_hex_color_passes_through(self):
        self._write_chip('date', '#[bg=#ff00ff] Mon ')
        chip = bm._make_chip('date', self._shm)
        self.assertEqual(chip['bg'], '#ff00ff')

    def test_light_bg_gives_dark_text(self):
        self._write_chip('load', '#[bg=brightwhite] 1.2 ')
        chip = bm._make_chip('load', self._shm)
        self.assertEqual(chip['color'], '#111111')

    def test_dark_bg_gives_light_text(self):
        self._write_chip('mem', '#[bg=black] 512M ')
        chip = bm._make_chip('mem', self._shm)
        self.assertEqual(chip['color'], '#eeeeee')

    def test_unknown_color_uses_fallback(self):
        self._write_chip('cpu', '#[bg=unknowncolor] 50% ')
        chip = bm._make_chip('cpu', self._shm)
        self.assertEqual(chip['bg'], '#2d2d2d')


# ---------------------------------------------------------------------------
# Byobu status — read_byobu_status integration
# ---------------------------------------------------------------------------

class TestReadByobuStatus(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._shm = Path(self._tmpdir.name)
        status_dir = self._shm / 'status.tmux'
        status_dir.mkdir()
        (status_dir / 'uptime').write_text('#[bg=blue] 1 day ')
        (status_dir / 'time').write_text('#[bg=cyan] 12:00 ')

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_returns_chips_when_shm_present(self):
        with patch.object(bm, '_byobu_shm', return_value=self._shm):
            with patch.object(bm, '_read_byobu_status_config',
                              return_value=(['uptime'], ['time'])):
                result = bm.read_byobu_status()
        self.assertEqual(len(result['left']), 1)
        self.assertEqual(len(result['right']), 1)
        self.assertEqual(result['left'][0]['label'], 'uptime')
        self.assertEqual(result['right'][0]['label'], 'time')

    def test_empty_when_no_shm(self):
        with patch.object(bm, '_byobu_shm', return_value=None):
            result = bm.read_byobu_status()
        self.assertEqual(result['left'], [])
        self.assertEqual(result['right'], [])

    def test_logo_is_filtered_out(self):
        with patch.object(bm, '_byobu_shm', return_value=self._shm):
            with patch.object(bm, '_read_byobu_status_config',
                              return_value=(['logo', 'uptime'], [])):
                result = bm.read_byobu_status()
        labels = [c['label'] for c in result['left']]
        self.assertNotIn('logo', labels)
        self.assertIn('uptime', labels)

    def test_missing_chip_file_skipped(self):
        with patch.object(bm, '_byobu_shm', return_value=self._shm):
            with patch.object(bm, '_read_byobu_status_config',
                              return_value=(['nonexistent_chip'], [])):
                result = bm.read_byobu_status()
        self.assertEqual(result['left'], [])


# ---------------------------------------------------------------------------
# Admin Unix socket protocol
# ---------------------------------------------------------------------------

class TestAdminSocket(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        _clear_sessions()
        bm._pair_code = ''
        bm._pair_attempts = 0
        bm._pair_code_mono_expiry = 0.0

    def tearDown(self):
        _clear_sessions()
        bm._pair_code = ''
        bm._pair_attempts = 0
        bm._pair_code_mono_expiry = 0.0

    async def _call(self, payload) -> dict:
        """Drive _handle_admin with mock reader/writer; return parsed response."""
        line = json.dumps(payload).encode() + b'\n'
        reader = asyncio.StreamReader()
        reader.feed_data(line)
        reader.feed_eof()
        written = []
        writer = MagicMock()
        writer.write = lambda data: written.append(data)
        writer.drain = AsyncMock()
        writer.close = MagicMock()
        writer.wait_closed = AsyncMock()
        await bm._handle_admin(reader, writer)
        return json.loads(b''.join(written).decode().strip())

    async def _call_raw(self, raw_bytes: bytes) -> dict:
        reader = asyncio.StreamReader()
        reader.feed_data(raw_bytes)
        reader.feed_eof()
        written = []
        writer = MagicMock()
        writer.write = lambda data: written.append(data)
        writer.drain = AsyncMock()
        writer.close = MagicMock()
        writer.wait_closed = AsyncMock()
        await bm._handle_admin(reader, writer)
        return json.loads(b''.join(written).decode().strip())

    async def test_pair_generate_returns_code(self):
        with patch.object(bm, '_print_pair_code'):
            resp = await self._call({'action': 'pair_generate'})
        self.assertIn('code', resp)
        self.assertIn('-', resp['code'])
        self.assertEqual(len(resp['code']), 7)  # XXX-XXX
        self.assertIn('expires_in', resp)
        self.assertTrue(bm._pair_code)

    async def test_sessions_list_empty(self):
        resp = await self._call({'action': 'sessions_list'})
        self.assertIsInstance(resp, list)
        self.assertEqual(len(resp), 0)

    async def test_sessions_list_populated(self):
        _add_session('tok_abcdef_123456')
        resp = await self._call({'action': 'sessions_list'})
        self.assertIsInstance(resp, list)
        self.assertEqual(len(resp), 1)
        entry = resp[0]
        self.assertIn('ip', entry)
        self.assertIn('paired_at', entry)
        self.assertIn('token', entry)
        self.assertIn('token_full', entry)
        # abbreviated token ends with …
        self.assertTrue(entry['token'].endswith('…'))

    async def test_sessions_delete_all(self):
        _add_session('tok1')
        _add_session('tok2')
        with patch.object(bm, '_save_tokens'):
            resp = await self._call({'action': 'sessions_delete', 'token': None})
        self.assertTrue(resp.get('ok'))
        self.assertEqual(resp.get('removed'), 2)
        self.assertEqual(len(bm._sessions), 0)

    async def test_sessions_delete_specific(self):
        _add_session('tok_gone')
        _add_session('tok_kept')
        with patch.object(bm, '_save_tokens'):
            resp = await self._call({'action': 'sessions_delete', 'token': 'tok_gone'})
        self.assertTrue(resp.get('ok'))
        self.assertNotIn('tok_gone', bm._sessions)
        self.assertIn('tok_kept', bm._sessions)

    async def test_sessions_delete_not_found(self):
        resp = await self._call({'action': 'sessions_delete', 'token': 'nosuchtoken'})
        self.assertIn('error', resp)

    async def test_sessions_delete_empty_token_errors(self):
        resp = await self._call({'action': 'sessions_delete', 'token': ''})
        self.assertIn('error', resp)

    async def test_unknown_action_returns_error(self):
        resp = await self._call({'action': 'do_something_impossible'})
        self.assertIn('error', resp)

    async def test_bad_json_returns_error(self):
        resp = await self._call_raw(b'not valid json\n')
        self.assertIn('error', resp)

    async def test_non_dict_json_returns_error(self):
        resp = await self._call([1, 2, 3])
        self.assertIn('error', resp)


# ---------------------------------------------------------------------------
# WebSocket handler
# ---------------------------------------------------------------------------

class TestWsHandler(AsyncHTTPTestCase):
    def get_app(self):
        return bm._make_app()

    def setUp(self):
        super().setUp()
        _clear_sessions()
        bm._pair_code = ''

    def tearDown(self):
        _clear_sessions()
        super().tearDown()

    def _ws_url(self, token=None):
        url = f'ws://localhost:{self.get_http_port()}/ws'
        if token:
            url += f'?token={token}'
        return url

    @gen_test(timeout=5)
    async def test_invalid_token_closes_connection(self):
        conn = await websocket_connect(self._ws_url(token='badtoken'))
        msg = await conn.read_message()
        self.assertIsNone(msg)

    @gen_test(timeout=5)
    async def test_valid_token_receives_initial_sessions(self):
        tok = _add_session('ws_tok_init')
        with patch.object(bm, 'tmux_list_sessions', return_value=[]):
            conn = await websocket_connect(self._ws_url(token=tok))
            msg = await conn.read_message()
        data = json.loads(msg)
        self.assertEqual(data['type'], 'sessions')
        conn.close()

    @gen_test(timeout=5)
    async def test_list_sessions_returns_sessions(self):
        tok = _add_session('ws_tok_ls')
        fake = [{'id': '$0', 'name': 'main', 'attached': True, 'windows': []}]
        with patch.object(bm, 'tmux_list_sessions', return_value=fake):
            conn = await websocket_connect(self._ws_url(token=tok))
            await conn.read_message()
            await conn.write_message(json.dumps({'type': 'list_sessions'}))
            resp = await conn.read_message()
        data = json.loads(resp)
        self.assertEqual(data['type'], 'sessions')
        self.assertEqual(data['data'], fake)
        conn.close()

    @gen_test(timeout=5)
    async def test_invalid_json_returns_error(self):
        tok = _add_session('ws_tok_json')
        with patch.object(bm, 'tmux_list_sessions', return_value=[]):
            conn = await websocket_connect(self._ws_url(token=tok))
            await conn.read_message()
            await conn.write_message('not valid json {{{')
            resp = await conn.read_message()
        data = json.loads(resp)
        self.assertEqual(data['type'], 'error')
        self.assertIn('invalid JSON', data['message'])
        conn.close()

    @gen_test(timeout=5)
    async def test_oversized_message_returns_error(self):
        tok = _add_session('ws_tok_size')
        with patch.object(bm, 'tmux_list_sessions', return_value=[]):
            conn = await websocket_connect(self._ws_url(token=tok))
            await conn.read_message()
            await conn.write_message('x' * 20_000)
            resp = await conn.read_message()
        data = json.loads(resp)
        self.assertEqual(data['type'], 'error')
        self.assertIn('too large', data['message'])
        conn.close()

    @gen_test(timeout=5)
    async def test_subscribe_invalid_pane_id_returns_error(self):
        tok = _add_session('ws_tok_sub')
        with patch.object(bm, 'tmux_list_sessions', return_value=[]):
            conn = await websocket_connect(self._ws_url(token=tok))
            await conn.read_message()
            await conn.write_message(json.dumps({'type': 'subscribe', 'pane_id': 'notvalid'}))
            resp = await conn.read_message()
        data = json.loads(resp)
        self.assertEqual(data['type'], 'error')
        conn.close()

    @gen_test(timeout=5)
    async def test_send_keys_invalid_pane_id_returns_error(self):
        tok = _add_session('ws_tok_sk')
        with patch.object(bm, 'tmux_list_sessions', return_value=[]):
            conn = await websocket_connect(self._ws_url(token=tok))
            await conn.read_message()
            await conn.write_message(json.dumps(
                {'type': 'send_keys', 'pane_id': 'bad', 'keys': 'ls'}))
            resp = await conn.read_message()
        data = json.loads(resp)
        self.assertEqual(data['type'], 'error')
        conn.close()

    @gen_test(timeout=5)
    async def test_new_session_empty_name_returns_error(self):
        tok = _add_session('ws_tok_ns')
        with patch.object(bm, 'tmux_list_sessions', return_value=[]):
            conn = await websocket_connect(self._ws_url(token=tok))
            await conn.read_message()
            await conn.write_message(json.dumps({'type': 'new_session', 'name': ''}))
            resp = await conn.read_message()
        data = json.loads(resp)
        self.assertEqual(data['type'], 'error')
        conn.close()

    @gen_test(timeout=5)
    async def test_new_window_invalid_session_id_returns_error(self):
        tok = _add_session('ws_tok_nw')
        with patch.object(bm, 'tmux_list_sessions', return_value=[]):
            conn = await websocket_connect(self._ws_url(token=tok))
            await conn.read_message()
            await conn.write_message(json.dumps(
                {'type': 'new_window', 'session_id': 'notvalid'}))
            resp = await conn.read_message()
        data = json.loads(resp)
        self.assertEqual(data['type'], 'error')
        conn.close()

    @gen_test(timeout=5)
    async def test_new_pane_invalid_window_id_returns_error(self):
        tok = _add_session('ws_tok_np')
        with patch.object(bm, 'tmux_list_sessions', return_value=[]):
            conn = await websocket_connect(self._ws_url(token=tok))
            await conn.read_message()
            await conn.write_message(json.dumps(
                {'type': 'new_pane', 'window_id': 'notvalid'}))
            resp = await conn.read_message()
        data = json.loads(resp)
        self.assertEqual(data['type'], 'error')
        conn.close()

    @gen_test(timeout=5)
    async def test_non_dict_json_returns_error(self):
        tok = _add_session('ws_tok_nd')
        with patch.object(bm, 'tmux_list_sessions', return_value=[]):
            conn = await websocket_connect(self._ws_url(token=tok))
            await conn.read_message()
            await conn.write_message(json.dumps([1, 2, 3]))
            resp = await conn.read_message()
        data = json.loads(resp)
        self.assertEqual(data['type'], 'error')
        conn.close()

    @gen_test(timeout=10)
    async def test_rate_limit_triggers_error(self):
        tok = _add_session('ws_tok_rate')
        with patch.object(bm, 'tmux_list_sessions', return_value=[]):
            conn = await websocket_connect(self._ws_url(token=tok))
            await conn.read_message()
            # Flood with messages exceeding the per-second rate limit
            burst = bm._WS_RATE_LIMIT + 5
            for _ in range(burst):
                await conn.write_message(json.dumps({'type': 'list_sessions'}))
            rate_errors = []
            for _ in range(burst):
                resp = await conn.read_message()
                if resp is None:
                    break
                d = json.loads(resp)
                if d.get('type') == 'error' and 'rate' in d.get('message', '').lower():
                    rate_errors.append(d)
                    break
        self.assertGreater(len(rate_errors), 0, 'Expected at least one rate-limit error')
        conn.close()


if __name__ == '__main__':
    unittest.main(verbosity=2)
