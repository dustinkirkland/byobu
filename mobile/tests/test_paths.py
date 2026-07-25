"""Tests for trustmux._paths — XDG base directories and instance isolation."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import trustmux._paths as paths


class BaseDirs(unittest.TestCase):
    """Give each test a clean, fully-overridden environment."""

    ENV = ("TRUSTMUX_CONFIG_DIR", "TRUSTMUX_STATE_DIR", "TRUSTMUX_INSTANCE",
           "XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_RUNTIME_DIR")

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.root = Path(self.td.name)
        self._env = patch.dict(os.environ, {}, clear=False)
        self._env.start()
        self.addCleanup(self._env.stop)
        for name in self.ENV:
            os.environ.pop(name, None)

    def home(self, *parts) -> Path:
        return Path.home().joinpath(*parts)


class TestXdgDefaults(BaseDirs):

    def test_defaults_follow_the_spec(self):
        self.assertEqual(paths.config_dir(), self.home(".config", "trustmux"))
        self.assertEqual(paths.state_dir(), self.home(".local", "state", "trustmux"))

    def test_xdg_vars_are_honoured(self):
        os.environ["XDG_CONFIG_HOME"] = str(self.root / "c")
        os.environ["XDG_STATE_HOME"] = str(self.root / "s")
        self.assertEqual(paths.config_dir(), self.root / "c" / "trustmux")
        self.assertEqual(paths.state_dir(), self.root / "s" / "trustmux")

    def test_trustmux_vars_win_over_xdg(self):
        os.environ["XDG_STATE_HOME"] = str(self.root / "ignored")
        os.environ["TRUSTMUX_STATE_DIR"] = str(self.root / "wins")
        self.assertEqual(paths.state_dir(), self.root / "wins")

    def test_xdg_runtime_dir_is_deliberately_not_used(self):
        # systemd-logind removes /run/user/$UID at last logout without
        # linger, which would strand a running daemon's socket.
        os.environ["XDG_RUNTIME_DIR"] = str(self.root / "runtime")
        inst = paths.Instance()
        for pth in (inst.sock, inst.pid_file):
            self.assertNotIn("runtime", str(pth))

    def test_machines_file_is_shared_config_not_per_instance(self):
        os.environ["TRUSTMUX_CONFIG_DIR"] = str(self.root / "c")
        self.assertEqual(paths.machines_file(), self.root / "c" / "machines.json")

    def test_blank_env_var_is_ignored(self):
        os.environ["TRUSTMUX_STATE_DIR"] = "   "
        self.assertEqual(paths.state_dir(), self.home(".local", "state", "trustmux"))


class TestInstancePaths(BaseDirs):

    def setUp(self):
        super().setUp()
        os.environ["TRUSTMUX_STATE_DIR"] = str(self.root / "state")

    def test_default_instance_is_not_special_cased(self):
        inst = paths.Instance()
        self.assertEqual(inst.name, "default")
        self.assertEqual(inst.state, self.root / "state" / "instances" / "default")

    def test_every_daemon_owned_file_lives_in_the_state_dir(self):
        inst = paths.Instance("work")
        for attr in ("tokens_file", "cert_file", "key_file", "log_file",
                     "sock", "pid_file"):
            self.assertEqual(getattr(inst, attr).parent, inst.state, attr)

    def test_nothing_the_daemon_writes_lands_in_the_config_dir(self):
        # The whole point of the change: config holds only machines.json.
        os.environ["TRUSTMUX_CONFIG_DIR"] = str(self.root / "config")
        inst = paths.Instance()
        for attr in ("tokens_file", "cert_file", "key_file", "log_file",
                     "sock", "pid_file"):
            self.assertFalse(
                str(getattr(inst, attr)).startswith(str(self.root / "config")), attr)

    def test_two_instances_share_nothing(self):
        a, b = paths.Instance("a"), paths.Instance("b")
        for attr in ("sock", "pid_file", "tokens_file", "log_file",
                     "cert_file", "key_file"):
            self.assertNotEqual(getattr(a, attr), getattr(b, attr), attr)

    def test_ensure_dirs_is_private(self):
        inst = paths.Instance("perms")
        inst.ensure_dirs()
        self.assertTrue(inst.state.is_dir())
        self.assertEqual(inst.state.stat().st_mode & 0o777, 0o700)

    def test_ensure_dirs_is_idempotent(self):
        inst = paths.Instance("twice")
        inst.ensure_dirs()
        inst.ensure_dirs()   # must not raise

    def test_label_only_set_for_non_default(self):
        self.assertEqual(paths.Instance().label(), "")
        self.assertEqual(paths.Instance("work").label(), " --instance work")


class TestResolveInstance(BaseDirs):

    def test_default_when_nothing_set(self):
        self.assertEqual(paths.resolve_instance().name, "default")

    def test_explicit_wins_over_env(self):
        os.environ["TRUSTMUX_INSTANCE"] = "fromenv"
        self.assertEqual(paths.resolve_instance("fromflag").name, "fromflag")

    def test_env_used_when_no_flag(self):
        os.environ["TRUSTMUX_INSTANCE"] = "fromenv"
        self.assertEqual(paths.resolve_instance().name, "fromenv")

    def test_accepts_reasonable_names(self):
        for name in ("work", "a", "A1", "my-box", "my_box", "v1.2", "x" * 32):
            self.assertEqual(paths.resolve_instance(name).name, name)

    def test_rejects_traversal_and_separators(self):
        for bad in ("..", ".", "../..", "a/b", "a/../b", "/abs", "a\\b",
                    ".hidden", "", "x" * 33, "sp ace", "semi;colon",
                    "$(id)", "`id`", "quo'te", "new\nline"):
            with self.subTest(name=bad):
                with patch("trustmux._paths.sys.stderr"):
                    with self.assertRaises(SystemExit) as cm:
                        paths.resolve_instance(bad)
                self.assertEqual(cm.exception.code, 2)

    def test_a_rejected_name_never_reaches_the_filesystem(self):
        os.environ["TRUSTMUX_STATE_DIR"] = str(self.root / "state")
        with patch("trustmux._paths.sys.stderr"):
            with self.assertRaises(SystemExit):
                paths.resolve_instance("../escape")
        self.assertEqual(list(self.root.rglob("escape")), [])


class TestSockPathLength(BaseDirs):

    def test_ok_for_a_normal_path(self):
        os.environ["TRUSTMUX_STATE_DIR"] = "/home/u/.local/state/trustmux"
        self.assertTrue(paths.check_sock_path(paths.Instance("work")))

    def test_rejects_an_overlong_path(self):
        os.environ["TRUSTMUX_STATE_DIR"] = "/" + "d" * 120
        import io
        buf = io.StringIO()
        self.assertFalse(paths.check_sock_path(paths.Instance("work"), buf))
        self.assertIn("unix socket", buf.getvalue())


class TestKnownInstances(BaseDirs):

    def setUp(self):
        super().setUp()
        os.environ["TRUSTMUX_STATE_DIR"] = str(self.root / "state")
        os.environ["TRUSTMUX_RUN_DIR"] = str(self.root / "run")

    def test_empty_before_anything_exists(self):
        self.assertEqual(paths.known_instances(), [])

    def test_lists_default_first_then_alphabetical(self):
        for name in ("zulu", "default", "alpha"):
            paths.Instance(name).ensure_dirs()
        self.assertEqual([i.name for i in paths.known_instances()],
                         ["default", "alpha", "zulu"])

    def test_ignores_stray_files(self):
        paths.Instance("real").ensure_dirs()
        (self.root / "state" / "instances" / "a-file").write_text("x")
        self.assertEqual([i.name for i in paths.known_instances()], ["real"])


class TestMigration(BaseDirs):

    def setUp(self):
        super().setUp()
        os.environ["TRUSTMUX_STATE_DIR"] = str(self.root / "state")
        self.legacy = self.root / "legacy"
        self.legacy.mkdir()
        os.environ["TRUSTMUX_CONFIG_DIR"] = str(self.legacy)

    def _legacy_file(self, name, content="x", mode=0o600):
        p = self.legacy / name
        p.write_text(content)
        p.chmod(mode)
        return p

    def test_no_op_when_nothing_to_migrate(self):
        self.assertFalse(paths.migrate_legacy_layout(stream=None))

    def test_moves_state_files_into_default_instance(self):
        self._legacy_file("tokens.json", '{"tok": {}}')
        self._legacy_file("cert.pem", "cert", 0o644)
        self._legacy_file("key.pem", "key")
        self._legacy_file("trustmux.log", "log")
        import io
        self.assertTrue(paths.migrate_legacy_layout(stream=io.StringIO()))
        target = paths.Instance().state
        self.assertEqual((target / "tokens.json").read_text(), '{"tok": {}}')
        self.assertEqual((target / "key.pem").read_text(), "key")
        self.assertEqual((target / "trustmux.log").read_text(), "log")

    def test_leaves_no_copy_behind(self):
        self._legacy_file("tokens.json")
        import io
        paths.migrate_legacy_layout(stream=io.StringIO())
        self.assertFalse((self.legacy / "tokens.json").exists())

    def test_preserves_0600_on_secrets(self):
        self._legacy_file("tokens.json", '{}', 0o600)
        self._legacy_file("key.pem", "key", 0o600)
        import io
        paths.migrate_legacy_layout(stream=io.StringIO())
        target = paths.Instance().state
        for name in ("tokens.json", "key.pem"):
            self.assertEqual((target / name).stat().st_mode & 0o777, 0o600, name)

    def test_target_directory_is_private(self):
        self._legacy_file("tokens.json")
        import io
        paths.migrate_legacy_layout(stream=io.StringIO())
        self.assertEqual(paths.Instance().state.stat().st_mode & 0o777, 0o700)

    def test_idempotent_second_run_does_nothing(self):
        self._legacy_file("tokens.json")
        import io
        self.assertTrue(paths.migrate_legacy_layout(stream=io.StringIO()))
        self.assertFalse(paths.migrate_legacy_layout(stream=io.StringIO()))

    def test_does_not_clobber_an_existing_instance(self):
        target = paths.Instance().state
        target.mkdir(parents=True)
        (target / "tokens.json").write_text("new")
        self._legacy_file("tokens.json", "old")
        self.assertFalse(paths.migrate_legacy_layout(stream=None))
        self.assertEqual((target / "tokens.json").read_text(), "new")

    def test_isolated_by_config_dir_override(self):
        # The legacy location follows $TRUSTMUX_CONFIG_DIR, so an overridden
        # run never reaches into the real ~/.config/trustmux.
        self.assertEqual(paths.legacy_dir(), self.legacy)

    def test_survives_a_cross_filesystem_move(self):
        # os.replace raises EXDEV when state lives on another filesystem.
        import errno, io
        self._legacy_file("tokens.json", "secret", 0o600)
        real_replace = os.replace

        def fake_replace(src, dst, *a, **kw):
            raise OSError(errno.EXDEV, "Invalid cross-device link")

        with patch("trustmux._paths.os.replace", side_effect=fake_replace):
            self.assertTrue(paths.migrate_legacy_layout(stream=io.StringIO()))
        target = paths.Instance().state / "tokens.json"
        self.assertEqual(target.read_text(), "secret")
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        self.assertFalse((self.legacy / "tokens.json").exists())

    def test_a_failed_move_does_not_raise(self):
        import io
        self._legacy_file("tokens.json")
        buf = io.StringIO()
        with patch("trustmux._paths._move_preserving_mode",
                   side_effect=OSError("boom")):
            self.assertFalse(paths.migrate_legacy_layout(stream=buf))
        self.assertIn("could not move", buf.getvalue())
        # Left in place for a retry rather than lost.
        self.assertTrue((self.legacy / "tokens.json").exists())

    def test_retries_only_what_is_still_pending(self):
        import io
        self._legacy_file("tokens.json", "tok")
        self._legacy_file("key.pem", "key")
        target = paths.Instance().state
        target.mkdir(parents=True)
        (target / "tokens.json").write_text("already there")
        self.assertTrue(paths.migrate_legacy_layout(stream=io.StringIO()))
        self.assertEqual((target / "tokens.json").read_text(), "already there")
        self.assertEqual((target / "key.pem").read_text(), "key")

    def test_leaves_legacy_socket_and_pidfile_alone(self):
        # A pre-upgrade daemon may still be serving on that socket.
        self._legacy_file("tokens.json")
        self._legacy_file("trustmux.sock")
        self._legacy_file("trustmux.pid", "123")
        import io
        paths.migrate_legacy_layout(stream=io.StringIO())
        self.assertTrue((self.legacy / "trustmux.sock").exists())
        self.assertTrue((self.legacy / "trustmux.pid").exists())



class TestMigrationHardening(BaseDirs):
    """Directory-migration code has a history of breaking long-time byobu
    users (LP #674217), so exercise the ugly cases, not just the happy path."""

    def setUp(self):
        super().setUp()
        os.environ["TRUSTMUX_STATE_DIR"] = str(self.root / "state")
        self.legacy = self.root / "legacy"
        self.legacy.mkdir()
        os.environ["TRUSTMUX_CONFIG_DIR"] = str(self.legacy)

    def _migrate(self):
        import io
        buf = io.StringIO()
        return paths.migrate_legacy_layout(stream=buf), buf.getvalue()

    def test_tokens_survive_byte_for_byte(self):
        # Losing a token means every paired phone silently stops working.
        blob = '{"tok_%s": {"ip": "10.0.0.1", "paired_at": 1.5}}' % ("x" * 300)
        (self.legacy / "tokens.json").write_text(blob)
        self._migrate()
        self.assertEqual((paths.Instance().state / "tokens.json").read_text(), blob)

    def test_binary_keypair_survives_unchanged(self):
        blob = bytes(range(256)) * 4
        (self.legacy / "key.pem").write_bytes(blob)
        self._migrate()
        self.assertEqual((paths.Instance().state / "key.pem").read_bytes(), blob)

    def test_unreadable_legacy_file_is_reported_not_fatal(self):
        (self.legacy / "tokens.json").write_text("secret")
        (self.legacy / "cert.pem").write_text("cert")
        with patch("trustmux._paths._move_preserving_mode",
                   side_effect=[None, OSError("permission denied")]):
            moved, msg = self._migrate()
        self.assertIn("could not move", msg)

    def test_unwritable_target_parent_is_reported_not_fatal(self):
        (self.legacy / "tokens.json").write_text("secret")
        with patch("trustmux._paths.Path.mkdir", side_effect=PermissionError("nope")):
            moved, msg = self._migrate()
        self.assertFalse(moved)
        self.assertIn("cannot create", msg)
        # The original is untouched, so a fixed-up retry still works.
        self.assertEqual((self.legacy / "tokens.json").read_text(), "secret")

    def test_a_legacy_symlink_is_followed_to_its_content(self):
        real = self.root / "elsewhere.json"
        real.write_text("via symlink")
        (self.legacy / "tokens.json").symlink_to(real)
        self._migrate()
        self.assertEqual((paths.Instance().state / "tokens.json").read_text(),
                         "via symlink")

    def test_partial_migration_completes_on_the_next_run(self):
        (self.legacy / "tokens.json").write_text("tok")
        (self.legacy / "key.pem").write_text("key")
        calls = {"n": 0}
        real = paths._move_preserving_mode

        def flaky(src, dst):
            calls["n"] += 1
            if calls["n"] == 2:
                raise OSError("transient")
            real(src, dst)

        with patch("trustmux._paths._move_preserving_mode", side_effect=flaky):
            self._migrate()
        self._migrate()                      # retry picks up what was left
        state = paths.Instance().state
        self.assertEqual((state / "tokens.json").read_text(), "tok")
        self.assertEqual((state / "key.pem").read_text(), "key")

    def test_nothing_is_moved_twice_over_a_repeated_run(self):
        (self.legacy / "tokens.json").write_text("first")
        self._migrate()
        # A daemon writes new tokens after the upgrade...
        (paths.Instance().state / "tokens.json").write_text("second")
        # ...and a stray legacy file reappears somehow.
        (self.legacy / "tokens.json").write_text("resurrected")
        self._migrate()
        self.assertEqual((paths.Instance().state / "tokens.json").read_text(),
                         "second")

    def test_config_only_install_migrates_nothing(self):
        # machines.json is config and must stay exactly where it is.
        (self.legacy / "machines.json").write_text("[]")
        moved, _ = self._migrate()
        self.assertFalse(moved)
        self.assertTrue((self.legacy / "machines.json").exists())


if __name__ == "__main__":
    unittest.main()
