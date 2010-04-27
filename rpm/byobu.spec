Name:           byobu
Version:        2.73
Release:        1%{?dist}
Summary:        a light-weight, configurable window manager built upon GNU screen

Group:          Applications/System
License:        GPL
URL:            http://launchpad.net/byobu
Source0:        http://code.launchpad.net/byobu/trunk/2.73/+download/byobu_2.73.orig.tar.gz
BuildRoot:      %{_tmppath}/%{name}-%{version}-%{release}-root-%(%{__id_u} -n)
BuildArch:	noarch

BuildRequires:  gettext
Requires:       screen, python >= 2.5, newt, gettext

%Description
Byobu is a Japanese term for decorative, multi-panel screens that serve as folding room dividers.
As an open source project, Byobu is an elegant enhancement of the otherwise functional, plain,
practical GNU Screen. Byobu includes an enhanced profile and configuration utilities for the GNU
screen window manager, such as toggle-able system status notifications.

# TODO
#   help.txt is in different locations under RPM/DEB
#   python 2.5 is not available on RHEL 5

%prep
%setup -q


%build

%install
rm -rf ${RPM_BUILD_ROOT}
mkdir -p ${RPM_BUILD_ROOT}
cp -ar etc ${RPM_BUILD_ROOT}/
cp -ar usr ${RPM_BUILD_ROOT}/
rm -rf ${RPM_BUILD_ROOT}/usr/share/doc

for po in po/*.po
do
    lang=${po#po/}
    lang=${lang%.po}
    mkdir -p ${RPM_BUILD_ROOT}/usr/share/locale/${lang}/LC_MESSAGES/
    msgfmt ${po} -o ${RPM_BUILD_ROOT}/usr/share/locale/${lang}/LC_MESSAGES/%{name}.mo
done

%clean
rm -rf $RPM_BUILD_ROOT


%files
%defattr(-,root,root,-)
%doc README
%doc COPYING
%doc usr/share/doc/%{name}/help.txt
%dir %{_datadir}/%{name}
%dir %{_prefix}/lib/%{name}
%dir %{_sysconfdir}/%{name}
%config %{_sysconfdir}/%{name}/*
%{_bindir}/%{name}*
%{_bindir}/motd+shell
%{_datadir}/applications/%{name}.desktop
%{_datadir}/%{name}/*
%{_datadir}/locale/*/LC_MESSAGES/%{name}.mo
%{_mandir}/man1/%{name}*.1.gz
%{_mandir}/man1/motd+shell.1.gz
%{_prefix}/lib/%{name}/*

%changelog
* Tue Feb 23 2010 Meethune Bhowmick <meethune@gmail.com>
- Simplify specfile to reflect new source layout

* Fri Aug  7 2009 Derek Carter <goozbach@friocorte.com>
- Updated specfile to build on Fedora11
- Fixed some keybindings for Fedora11
- Made an expermental trigger for sourcing config on exit of byobu-config

* Tue May  5 2009 David Duffey <email@davidduffey.com>
- Initial RPM release
- see /usr/share/doc/byobu-*/changelog for upstream changelog
