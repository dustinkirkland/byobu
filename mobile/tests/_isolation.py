"""Isolate the suite from the developer's real trustmux state and tmux.

test_ctl exercises Instance directories, pid files and the legacy-layout
cleanup against whatever the path helpers resolve; without isolation a
suite run rmtrees the real ~/.local/state/trustmux, taking the live
daemon's socket, log and pairing tokens with it. Point every base the
helpers consult at a per-run temp dir before any test module imports the
package. test_paths overrides these vars itself where the resolution order
is the thing under test.

Every test module imports this first, by both names it can have: the
project's test command, `python3 -m unittest discover -s tests`, imports
the modules top-level (test_ctl, not tests.test_ctl), so the tests
package's own __init__.py never runs under it. This module did live there,
and so isolated nothing under that command.
"""
import atexit
import os
import shutil
import tempfile

if not os.environ.get("TRUSTMUX_TESTS_ISOLATED"):
    os.environ["TRUSTMUX_TESTS_ISOLATED"] = "1"
    _tmp = tempfile.mkdtemp(prefix="trustmux-tests-")
    atexit.register(shutil.rmtree, _tmp, ignore_errors=True)
    for _var in ("XDG_STATE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME"):
        os.environ[_var] = os.path.join(_tmp, _var.lower())

    # And from the developer's real tmux server. A test that mocks the capture
    # but not a step before it (the control client looks up the pane's session
    # with a real `tmux display-message`) would otherwise reach whatever server
    # $TMUX names -- inside byobu, the live one -- and attach a control client
    # to it, its replies standing in for the mock's: tests that pass in Docker
    # fail at random on a desktop. With $TMUX unset and TMUX_TMPDIR an empty
    # directory, any tmux a test reaches finds no server and fails fast.
    os.environ.pop("TMUX", None)
    os.environ.pop("TMUX_PANE", None)
    os.environ["TMUX_TMPDIR"] = os.path.join(_tmp, "tmux")
    os.makedirs(os.environ["TMUX_TMPDIR"])
