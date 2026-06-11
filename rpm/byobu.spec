%global _iconstheme    hicolor
%global _iconsbasedir  %{_datadir}/icons/%{_iconstheme}
%global _iconsscaldir  %{_iconsbasedir}/scalable/apps
%global trustmuxlibdir %{_prefix}/lib/trustmux

Name:		byobu
Version:	7.11
Release:	1%{?dist}
Summary:	Light-weight, configurable window manager built upon GNU screen

License:	GPL-3.0-only
URL:		https://github.com/dustinkirkland/byobu
Source0:	%{url}/archive/refs/tags/%{version}.tar.gz#/%{name}-%{version}.tar.gz

# Default windows examples (Fedora-specific)
Source1:	fedoracommon

BuildArch:	noarch
BuildRequires:	automake
BuildRequires:	desktop-file-utils
BuildRequires:	make
BuildRequires:	gettext
BuildRequires:	python3-devel
Requires:	gettext-runtime
Requires:	newt
Requires:	openssl
Requires:	python3-newt
Requires:	python3-tornado
Requires:	screen
Requires:	tmux

%description
Byobu is a Japanese term for decorative, multi-panel screens that serve
as folding room dividers. As an open source project, Byobu is an
elegant enhancement of the otherwise functional, plain,
practical GNU Screen. Byobu includes an enhanced profile
and configuration utilities for the GNU screen window manager,
such as toggle-able system status notifications.

%prep
%autosetup

# remove swap file
rm -f usr/bin/.byobu-status-print.swp

# fix path for lib directory in scripts
grep -rl '{BYOBU_PREFIX}/lib/' . | xargs sed -i "s#{BYOBU_PREFIX}/lib/#{BYOBU_PREFIX}/libexec/#g"
grep -rl 'BYOBU_PREFIX/lib' . | xargs sed -i "s#BYOBU_PREFIX/lib/#BYOBU_PREFIX/libexec/#g"

# fix path for correct directory in /usr/share
sed -i "s#DOC = BYOBU_PREFIX + '/share/doc/' + PKG#DOC='%{_docdir}/%{name}'#g" usr/lib/byobu/include/config.py.in

# set default fedora windows
cp -p %{SOURCE1} usr/share/byobu/windows/common

# fix path from lib to libexec by modified Makefile.am and in
sed -i "s#/lib/#/libexec/#g" usr/lib/byobu/Makefile.am
sed -i "s#/lib/#/libexec/#g" usr/lib/byobu/include/Makefile.am


%build
export BYOBU_PYTHON=%{__python3}
autoreconf -fiv
%configure
%make_build


%install
%make_install

# remove apport which is not available in fedora
rm %{buildroot}%{_libexecdir}/%{name}/apport
sed -i 's#status\[\"apport\"\]=0##g' %{buildroot}%{_bindir}/byobu-config

# install translations manually as the Makefile doesn't do it
for po in po/*.po
do
    lang=${po#po/}
    lang=${lang%.po}
    mkdir -p %{buildroot}%{_datadir}/locale/${lang}/LC_MESSAGES/
    msgfmt ${po} -o %{buildroot}%{_datadir}/locale/${lang}/LC_MESSAGES/%{name}.mo
done

%find_lang %{name}

# use the old xterm .desktop style for while
cp -a usr/share/%{name}/desktop/%{name}.desktop.old usr/share/%{name}/desktop/%{name}.desktop
desktop-file-install usr/share/%{name}/desktop/%{name}.desktop --dir %{buildroot}%{_datadir}/applications

# remove vigpg
rm %{buildroot}%{_bindir}/vigpg
rm %{buildroot}%{_mandir}/man1/vigpg.1*

# add icon into /usr/share/icons/hicolor/scalable/apps/ from /usr/share/byobu/pixmaps/byobu.svg
install -D -p -m 0644 usr/share/byobu/pixmaps/byobu.svg %{buildroot}%{_iconsscaldir}/%{name}.svg

# fix shebangs
%py3_shebang_fix %{buildroot}%{_bindir}/*
%py3_shebang_fix %{buildroot}%{_libexecdir}/%{name}/*
%py3_shebang_fix %{buildroot}%{trustmuxlibdir}/trustmux/_daemon.py

# install README.md manually to the doc dir
install -d %{buildroot}%{_docdir}/%{name}
cp -p README.md %{buildroot}%{_docdir}/%{name}/

# remove empty profile.d
rm -rf %{buildroot}%{_sysconfdir}/profile.d


%check
desktop-file-validate %{buildroot}%{_datadir}/applications/%{name}.desktop


%files -f %{name}.lang
%license COPYING
%{_docdir}/%{name}/
%{_bindir}/%{name}*
%{_bindir}/col1
%{_bindir}/ctail
%{_bindir}/manifest
%{_bindir}/purge-old-kernels
%{_bindir}/wifi-status
%{_datadir}/%{name}/
%{_datadir}/applications/%{name}.desktop
%{_datadir}/sounds/%{name}/
%{_datadir}/dbus-1/services/us.kirkland.terminals.byobu.service
%{_iconsscaldir}/%{name}.svg
%{_mandir}/man1/%{name}*.1*
%{_mandir}/man1/col1.1*
%{_mandir}/man1/ctail.1*
%{_mandir}/man1/manifest.1*
%{_mandir}/man1/purge-old-kernels.1*
%{_mandir}/man1/wifi-status.1*
%{_libexecdir}/%{name}/
%config(noreplace) %{_sysconfdir}/%{name}/
# trustmux files
%{_bindir}/trustmux
%{_bindir}/trustmuxd
%{trustmuxlibdir}/trustmux/__init__.py
%{trustmuxlibdir}/trustmux/__main__.py
%{trustmuxlibdir}/trustmux/_ctl.py
%{trustmuxlibdir}/trustmux/_daemon.py
%{trustmuxlibdir}/trustmux/_disable.py
%{trustmuxlibdir}/trustmux/_enable.py
%{trustmuxlibdir}/trustmux/_pair.py
%{trustmuxlibdir}/trustmux/_unpair.py
%{_datadir}/bash-completion/completions/trustmux
%{_mandir}/man1/trustmux.1.gz
%{_mandir}/man1/trustmuxd.1.gz
%{_datadir}/trustmux/static/app.js
%{_datadir}/trustmux/static/index.html
%{_datadir}/trustmux/static/sw.js
%{_datadir}/trustmux/static/trustmux-logo.svg
%{_datadir}/trustmux/static/trustmux.svg
%{_datadir}/trustmux/static/icons/icon-192.png
%{_datadir}/trustmux/static/icons/icon-512.png


%changelog
* Tue Jun 10 2026 Dustin Kirkland <dustin.kirkland@gmail.com> - 7.11-1
- Update to 7.11; add trustmux files; import spec from Fedora dist-git
  (maintained by Filipe Rosset <rosset.filipe@gmail.com>)
