"""Isolate the suite from the developer's real trustmux state.

test_ctl exercises Instance directories, pid files and the legacy-layout
cleanup against whatever the path helpers resolve; without isolation a
suite run rmtrees the real ~/.local/state/trustmux, taking the live
daemon's socket, log and pairing tokens with it. Point every base the
helpers consult at a per-run temp dir before any test module imports the
package (module-level so it precedes collection-time imports; the daemon
module snapshots Instance() at import). test_paths overrides these vars
itself where the resolution order is the thing under test.
"""
import os
import tempfile

_tmp = tempfile.mkdtemp(prefix="trustmux-tests-")
for _var in ("XDG_STATE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME"):
    os.environ[_var] = os.path.join(_tmp, _var.lower())
