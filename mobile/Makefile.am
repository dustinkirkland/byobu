# Trustmux — mobile companion daemon for byobu/tmux.
#
# The wrappers in $(bindir) import the trustmux Python package from a fixed
# location (trustmuxpydir), so we install the modules to a flat directory
# tree rather than into Python's site-packages.  This keeps autotools-based
# installs decoupled from any system Python's site-packages layout, and
# matches what downstream distro packaging has historically done.
#
# A parallel pip/PyPI distribution is built from this same source via
# pyproject.toml; the two installation paths are independent.

# Use $(prefix)/lib explicitly rather than $(libdir) so that Debian
# multiarch builds (libdir=/usr/lib/x86_64-linux-gnu) still land Python
# modules in the arch-independent /usr/lib/trustmux that the wrappers
# expect — matching the historical Debian layout.
trustmuxpydir = $(prefix)/lib/trustmux
trustmuxmoddir = $(trustmuxpydir)/trustmux
dist_trustmuxmod_DATA = \
	trustmux/__init__.py \
	trustmux/__main__.py \
	trustmux/_daemon.py \
	trustmux/_ctl.py \
	trustmux/_pair.py \
	trustmux/_unpair.py \
	trustmux/_enable.py \
	trustmux/_disable.py

trustmuxstaticdir = $(datadir)/trustmux/static
dist_trustmuxstatic_DATA = \
	trustmux/static/index.html \
	trustmux/static/app.js \
	trustmux/static/sw.js \
	trustmux/static/trustmux.svg \
	trustmux/static/trustmux-logo.svg

trustmuxiconsdir = $(datadir)/trustmux/static/icons
dist_trustmuxicons_DATA = \
	trustmux/static/icons/icon-192.png \
	trustmux/static/icons/icon-512.png

dist_man1_MANS = \
	man/man1/trustmux.1 \
	man/man1/trustmux-ctl.1 \
	man/man1/trustmux-pair.1 \
	man/man1/trustmux-unpair.1 \
	man/man1/trustmux-enable.1 \
	man/man1/trustmux-disable.1

# Generated wrappers — sed-substitute @trustmuxpydir@ at build time so
# the path resolves to the actual configure --prefix.  Using Make rules
# rather than AC_CONFIG_FILES because $(libdir) is only fully expanded
# at Make time, not configure time.
WRAPPERS = \
	trustmux-ctl \
	trustmux-pair \
	trustmux-unpair \
	trustmux-enable \
	trustmux-disable \
	trustmux-daemon

bin_SCRIPTS = $(WRAPPERS:%=%.gen)

EXTRA_DIST = \
	trustmux-ctl.in \
	trustmux-pair.in \
	trustmux-unpair.in \
	trustmux-enable.in \
	trustmux-disable.in \
	trustmux-daemon.in \
	pyproject.toml \
	requirements.txt \
	uv.lock \
	README.md \
	generate_screenshots.py \
	tests/__init__.py \
	tests/test_ctl.py \
	tests/test_daemon.py \
	tests/test_daemon_extended.py

CLEANFILES = $(WRAPPERS:%=%.gen)

do_subst = sed -e 's|@trustmuxpydir[@]|$(trustmuxpydir)|g'

%.gen: %.in Makefile
	$(do_subst) < $(srcdir)/$*.in > $@
	chmod +x $@

# Install with the right names (strip the .gen suffix) and rename
# trustmux-daemon → trustmux to match Debian's historical layout, where
# /usr/bin/trustmux is the user-facing entry point for the daemon.
install-exec-hook:
	cd $(DESTDIR)$(bindir) && \
	  for w in trustmux-ctl trustmux-pair trustmux-unpair \
	           trustmux-enable trustmux-disable; do \
	    mv $$w.gen $$w; \
	  done && \
	  mv trustmux-daemon.gen trustmux

uninstall-hook:
	cd $(DESTDIR)$(bindir) && rm -f \
	  trustmux trustmux-ctl trustmux-pair trustmux-unpair \
	  trustmux-enable trustmux-disable
