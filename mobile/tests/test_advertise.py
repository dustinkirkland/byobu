"""Tests for trustmux._advertise."""

import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import trustmux._advertise as adv
from trustmux._paths import Instance

_tmp_root = None
_tmp_env = None


def setUpModule():
    # Root the base directories in a temp tree: _from_config() reads the
    # instance config file, which must never be the developer's real one.
    global _tmp_root, _tmp_env
    _tmp_root = tempfile.TemporaryDirectory()
    root = Path(_tmp_root.name)
    _tmp_env = patch.dict(os.environ, {
        'TRUSTMUX_CONFIG_DIR': str(root / 'config'),
        'TRUSTMUX_STATE_DIR':  str(root / 'state'),
    })
    _tmp_env.start()
    os.environ.pop('TRUSTMUX_ADVERTISE', None)


def tearDownModule():
    _tmp_env.stop()
    _tmp_root.cleanup()


def _script(body: str, mode: int = 0o755) -> str:
    """Write an executable script and return its absolute path."""
    d = tempfile.mkdtemp(dir=_tmp_root.name)
    path = Path(d) / "prog"
    path.write_text(body)
    path.chmod(mode)
    return str(path)


class TestNormalizeLiterals(unittest.TestCase):

    def test_bare_ipv4_takes_scheme_and_port_from_daemon(self):
        self.assertEqual(adv.normalize('34.1.2.3', 'https', 7432),
                         ('https://34.1.2.3:7432/', '34.1.2.3'))

    def test_bare_name(self):
        self.assertEqual(adv.normalize('tmux.example.com', 'https', 7432),
                         ('https://tmux.example.com:7432/', 'tmux.example.com'))

    def test_explicit_port_beats_the_daemon_port(self):
        self.assertEqual(adv.normalize('host:8443', 'https', 7432).url,
                         'https://host:8443/')

    def test_default_port_is_not_shown(self):
        self.assertEqual(adv.normalize('host', 'https', 443).url, 'https://host/')
        self.assertEqual(adv.normalize('host', 'http', 80).url, 'http://host/')

    def test_trailing_dot_dropped_for_the_certificate(self):
        self.assertEqual(adv.normalize('host.example.com.', 'https', 443).host,
                         'host.example.com')

    def test_bare_ipv6_is_bracketed_in_the_url_only(self):
        got = adv.normalize('2001:db8::1', 'https', 7432)
        self.assertEqual(got, ('https://[2001:db8::1]:7432/', '2001:db8::1'))

    def test_bracketed_ipv6_with_port(self):
        self.assertEqual(adv.normalize('[2001:db8::1]:8443', 'https', 7432).url,
                         'https://[2001:db8::1]:8443/')

    def test_rejects_empty(self):
        with self.assertRaisesRegex(adv.AdvertiseError, 'blank line'):
            adv.normalize('')

    def test_rejects_whitespace_padding(self):
        with self.assertRaises(adv.AdvertiseError):
            adv.normalize(' host ', 'https', 443)

    def test_rejects_wildcard_bind_address(self):
        for value in ('0.0.0.0', '::'):
            with self.assertRaisesRegex(adv.AdvertiseError, 'wildcard'):
                adv.normalize(value, 'https', 7432)

    def test_rejects_bad_port(self):
        for value in ('host:0', 'host:70000', 'host:nope'):
            with self.assertRaisesRegex(adv.AdvertiseError, 'port'):
                adv.normalize(value, 'https', 7432)

    def test_rejects_unusable_hostname(self):
        for value in ('-host', 'ho st', 'host..example', 'host_1'):
            with self.assertRaises(adv.AdvertiseError):
                adv.normalize(value, 'https', 7432)

    def test_rejects_unbalanced_bracket(self):
        with self.assertRaisesRegex(adv.AdvertiseError, "unbalanced"):
            adv.normalize('[2001:db8::1', 'https', 7432)


class TestNormalizeUrls(unittest.TestCase):

    def test_full_url_overrides_scheme_and_port(self):
        # The proxy-on-443 case: neither the daemon's port nor its scheme applies.
        self.assertEqual(adv.normalize('https://tmux.example.com/', 'http', 7432),
                         ('https://tmux.example.com/', 'tmux.example.com'))

    def test_url_without_trailing_slash(self):
        self.assertEqual(adv.normalize('https://host:8443', 'https', 7432).url,
                         'https://host:8443/')

    def test_rejects_other_schemes(self):
        with self.assertRaisesRegex(adv.AdvertiseError, 'scheme'):
            adv.normalize('ftp://host/')

    def test_rejects_a_path(self):
        with self.assertRaisesRegex(adv.AdvertiseError, 'root only'):
            adv.normalize('https://host/trustmux')

    def test_rejects_a_fragment_since_pairing_uses_it(self):
        with self.assertRaisesRegex(adv.AdvertiseError, 'fragment'):
            adv.normalize('https://host/#123456')

    def test_rejects_credentials(self):
        with self.assertRaisesRegex(adv.AdvertiseError, 'credentials'):
            adv.normalize('https://user:pw@host/')

    def test_rejects_invalid_port(self):
        with self.assertRaisesRegex(adv.AdvertiseError, 'port'):
            adv.normalize('https://host:99999/')


class TestResolveCmd(unittest.TestCase):

    def test_one_value_per_line(self):
        prog = _script('#!/bin/sh\nprintf "34.1.2.3\\ntmux.example.com\\n"\n')
        got = adv.resolve([f'cmd:{prog}'], 'https', 7432)
        self.assertEqual([a.url for a in got],
                         ['https://34.1.2.3:7432/', 'https://tmux.example.com:7432/'])
        self.assertEqual([a.host for a in got], ['34.1.2.3', 'tmux.example.com'])

    def test_quoted_argument_survives_without_a_shell(self):
        prog = _script('#!/bin/sh\ntest "$1" = "a b" && echo host.example.com\n')
        got = adv.resolve([f'cmd:{prog} "a b"'], 'https', 443)
        self.assertEqual([a.url for a in got], ['https://host.example.com/'])

    def test_nonzero_exit_is_fatal_and_quotes_stderr(self):
        prog = _script('#!/bin/sh\necho "no metadata" >&2\nexit 7\n')
        with self.assertRaisesRegex(adv.AdvertiseError, 'exited 7: no metadata'):
            adv.resolve([f'cmd:{prog}'])

    def test_blank_line_is_fatal_not_skipped(self):
        # The shell-pipeline failure mode: a wrapper that swallows an error and
        # prints an empty line must not read as "nothing to advertise".
        prog = _script('#!/bin/sh\necho ""\n')
        with self.assertRaisesRegex(adv.AdvertiseError, 'blank line'):
            adv.resolve([f'cmd:{prog}'])

    def test_no_output_is_fatal(self):
        prog = _script('#!/bin/sh\nexit 0\n')
        with self.assertRaisesRegex(adv.AdvertiseError, 'printed nothing'):
            adv.resolve([f'cmd:{prog}'])

    def test_unusable_line_names_the_source(self):
        prog = _script('#!/bin/sh\necho "not a host!"\n')
        with self.assertRaisesRegex(adv.AdvertiseError, prog):
            adv.resolve([f'cmd:{prog}'])

    def test_error_page_on_stdout_is_rejected(self):
        prog = _script('#!/bin/sh\necho "<html>404 Not Found</html>"\n')
        with self.assertRaises(adv.AdvertiseError):
            adv.resolve([f'cmd:{prog}'])

    def test_timeout_is_fatal(self):
        prog = _script('#!/bin/sh\nsleep 30\n')
        with patch.object(adv, 'CMD_BUDGET', 0.4):
            with self.assertRaisesRegex(adv.AdvertiseError, 'still running'):
                adv.resolve([f'cmd:{prog}'])

    def test_missing_program_is_fatal(self):
        with self.assertRaisesRegex(adv.AdvertiseError, 'not found'):
            adv.resolve(['cmd:/nonexistent/trustmux-test-prog'])

    def test_non_executable_program_is_fatal(self):
        prog = _script('#!/bin/sh\necho host\n', mode=0o644)
        with self.assertRaises(adv.AdvertiseError):
            adv.resolve([f'cmd:{prog}'])

    def test_shell_metacharacters_are_refused_not_passed_through(self):
        for spec in ('cmd:/bin/echo host | tr -d x',
                     'cmd:/bin/echo $(hostname)',
                     'cmd:/bin/echo a; /bin/echo b',
                     'cmd:/bin/echo `hostname`'):
            with self.assertRaisesRegex(adv.AdvertiseError, 'does not use a shell'):
                adv.resolve([spec])

    def test_unbalanced_quote_is_fatal(self):
        with self.assertRaisesRegex(adv.AdvertiseError, 'cannot parse'):
            adv.resolve(['cmd:/bin/echo "unclosed'])

    def test_empty_command_is_fatal(self):
        with self.assertRaisesRegex(adv.AdvertiseError, 'names no command'):
            adv.resolve(['cmd:   '])

    def test_output_cannot_recurse_into_another_cmd(self):
        prog = _script('#!/bin/sh\necho "cmd:/bin/echo host"\n')
        with self.assertRaisesRegex(adv.AdvertiseError, 'cannot produce another'):
            adv.resolve([f'cmd:{prog}'])

    def test_oversized_output_is_fatal(self):
        prog = _script('#!/bin/sh\nyes host.example.com | head -n 20000\n')
        with self.assertRaises(adv.AdvertiseError):
            adv.resolve([f'cmd:{prog}'])

    def test_too_many_values_is_fatal(self):
        prog = _script('#!/bin/sh\nfor i in $(seq 1 40); do echo "h$i.example.com"; done\n')
        with self.assertRaisesRegex(adv.AdvertiseError, 'more than'):
            adv.resolve([f'cmd:{prog}'])

    def test_sources_share_one_budget(self):
        # Two sources that each fit the budget alone must not together exceed
        # the wait _ctl._launch() is willing to give the daemon.
        prog = _script('#!/bin/sh\nsleep 1\necho host.example.com\n')
        with patch.object(adv, 'CMD_BUDGET', 1.4):
            with self.assertRaisesRegex(adv.AdvertiseError, 'out of time|still running'):
                adv.resolve([f'cmd:{prog}', f'cmd:{prog}'])


class TestResolveOrderAndDedup(unittest.TestCase):

    def test_first_value_is_first(self):
        got = adv.resolve(['a.example.com', 'b.example.com'], 'https', 443)
        self.assertEqual([a.host for a in got], ['a.example.com', 'b.example.com'])

    def test_repeated_host_is_dropped_keeping_the_first(self):
        got = adv.resolve(['host:1234', 'host:5678'], 'https', 443)
        self.assertEqual([a.url for a in got], ['https://host:1234/'])

    def test_literal_and_cmd_sources_mix(self):
        prog = _script('#!/bin/sh\necho 34.1.2.3\n')
        got = adv.resolve(['tmux.example.com', f'cmd:{prog}'], 'https', 7432)
        self.assertEqual([a.host for a in got], ['tmux.example.com', '34.1.2.3'])

    def test_no_sources_resolves_to_nothing(self):
        self.assertEqual(adv.resolve([]), [])


class TestCheckSources(unittest.TestCase):
    """Validation the CLI can do before launching anything."""

    def test_accepts_what_resolve_would(self):
        adv.check_sources(['34.1.2.3', 'https://host/', 'cmd:/bin/echo host'])

    def test_catches_a_shell_pipeline(self):
        with self.assertRaisesRegex(adv.AdvertiseError, 'does not use a shell'):
            adv.check_sources(['cmd:/bin/echo host | tr -d x'])

    def test_catches_a_mistyped_literal(self):
        with self.assertRaisesRegex(adv.AdvertiseError, 'root only'):
            adv.check_sources(['https://host/trustmux'])

    def test_runs_nothing(self):
        # Whether the program works is the daemon's business; a source naming
        # one that does not exist yet must not fail the command that starts it.
        with patch('subprocess.run') as run:
            adv.check_sources(['cmd:/nonexistent/prog'])
        run.assert_not_called()


class TestResolveSources(unittest.TestCase):

    def setUp(self):
        self.inst = Instance('advtest')
        self.inst.config_file.parent.mkdir(parents=True, exist_ok=True)
        self.addCleanup(lambda: self.inst.config_file.unlink(missing_ok=True))

    def _config(self, obj, mode: int = 0o600):
        self.inst.config_file.write_text(json.dumps(obj))
        self.inst.config_file.chmod(mode)

    def test_flag_wins_and_replaces_rather_than_appends(self):
        self._config({'advertise': ['stale.example.com']})
        with patch.dict(os.environ, {'TRUSTMUX_ADVERTISE': 'env.example.com'}):
            got = adv.resolve_sources(['flag.example.com'], False, self.inst)
        self.assertEqual(got, ['flag.example.com'])

    def test_env_wins_over_config(self):
        self._config({'advertise': ['stale.example.com']})
        with patch.dict(os.environ, {'TRUSTMUX_ADVERTISE': 'env.example.com'}):
            self.assertEqual(adv.resolve_sources(None, False, self.inst),
                             ['env.example.com'])

    def test_env_is_a_single_source_not_a_list(self):
        # A cmd: source can hold spaces and commas, so no delimiter is safe.
        with patch.dict(os.environ, {'TRUSTMUX_ADVERTISE': 'cmd:/bin/prog --a b'}):
            self.assertEqual(adv.resolve_sources(None, False, self.inst),
                             ['cmd:/bin/prog --a b'])

    def test_blank_env_is_unset_not_an_error(self):
        self._config({'advertise': ['cfg.example.com']})
        with patch.dict(os.environ, {'TRUSTMUX_ADVERTISE': '  '}):
            self.assertEqual(adv.resolve_sources(None, False, self.inst),
                             ['cfg.example.com'])

    def test_config_list(self):
        self._config({'advertise': ['a.example.com', 'cmd:/bin/prog']})
        self.assertEqual(adv.resolve_sources(None, False, self.inst),
                         ['a.example.com', 'cmd:/bin/prog'])

    def test_config_accepts_a_bare_string(self):
        self._config({'advertise': 'a.example.com'})
        self.assertEqual(adv.resolve_sources(None, False, self.inst),
                         ['a.example.com'])

    def test_absent_config_is_not_an_error(self):
        self.assertEqual(adv.resolve_sources(None, False, self.inst), [])

    def test_config_without_the_key_is_not_an_error(self):
        self._config({'something-else': 1})
        self.assertEqual(adv.resolve_sources(None, False, self.inst), [])

    def test_group_writable_config_is_refused(self):
        # It can name a program to run, so anyone who can write it can choose
        # what this daemon executes.
        self._config({'advertise': ['cmd:/bin/prog']}, mode=0o660)
        with self.assertRaisesRegex(adv.AdvertiseError, 'writable by group'):
            adv.resolve_sources(None, False, self.inst)

    def test_world_writable_config_is_refused(self):
        self._config({'advertise': ['a.example.com']}, mode=0o606)
        with self.assertRaisesRegex(adv.AdvertiseError, 'writable by group'):
            adv.resolve_sources(None, False, self.inst)

    def test_malformed_config_is_an_error(self):
        self.inst.config_file.write_text('{not json')
        self.inst.config_file.chmod(0o600)
        with self.assertRaisesRegex(adv.AdvertiseError, 'cannot read'):
            adv.resolve_sources(None, False, self.inst)

    def test_wrong_shape_is_an_error(self):
        self._config({'advertise': [1, 2]})
        with self.assertRaisesRegex(adv.AdvertiseError, 'list'):
            adv.resolve_sources(None, False, self.inst)

    def test_no_advertise_overrides_a_configured_source(self):
        self._config({'advertise': ['cfg.example.com']})
        self.assertEqual(adv.resolve_sources(None, True, self.inst), [])

    def test_no_advertise_with_advertise_is_an_error(self):
        with self.assertRaisesRegex(adv.AdvertiseError, 'mutually exclusive'):
            adv.resolve_sources(['host.example.com'], True, self.inst)

    def test_empty_flag_value_is_an_error(self):
        with self.assertRaisesRegex(adv.AdvertiseError, 'needs a value'):
            adv.resolve_sources([''], False, self.inst)


class TestAdvertisedUrls(unittest.TestCase):
    """Shaped defensively: this crosses the admin socket."""

    def test_reads_the_list(self):
        self.assertEqual(adv.advertised_urls({'advertise': ['https://h/']}),
                         ['https://h/'])

    def test_missing_or_wrong_type_is_empty(self):
        for info in (None, {}, {'advertise': None}, {'advertise': 'https://h/'},
                     {'advertise': 7}):
            self.assertEqual(adv.advertised_urls(info), [])

    def test_non_string_members_are_dropped(self):
        self.assertEqual(adv.advertised_urls({'advertise': ['https://h/', 1, '']}),
                         ['https://h/'])


if __name__ == '__main__':
    unittest.main()
