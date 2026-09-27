"""Isolate the suite from the developer's real trustmux state.

test_ctl exercises Instance directories, pid files and the legacy-layout
cleanup against whatever the path helpers resolve; without isolation a
suite run rmtrees the real ~/.local/state/trustmux, taking the live
daemon's socket, log and pairing tokens with it. Point every base the
helpers consult at a per-run temp dir before any test module imports the
package (module-level, in this package's own __init__.py rather than a
conftest.py: a conftest.py only auto-loads under pytest, but the project's
actual test command is `python3 -m unittest discover` -- which has no
conftest.py mechanism at all and would silently skip the isolation. This
package's __init__.py runs under both: importing any tests.test_* module
first imports tests, unittest discover included). test_paths overrides
these vars itself where the resolution order is the thing under test.
"""
import atexit
import os
import shutil
import tempfile

_tmp = tempfile.mkdtemp(prefix="trustmux-tests-")
atexit.register(shutil.rmtree, _tmp, ignore_errors=True)
for _var in ("XDG_STATE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME"):
    os.environ[_var] = os.path.join(_tmp, _var.lower())
