Name:           byobu
Version:        2.53
Release:        1%{?dist}
Summary:        a set of useful profiles and a profile-switcher for GNU screen

Group:          Applications/System
License:        GPL
URL:            http://launchpad.net/byobu
Source0:        http://code.launchpad.net/byobu/trunk/2.53/+download/byobu_2.53.orig.tar.gz
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
%setup -q -n %{name}_%{version}.orig


%build

%install
rm -rf $RPM_BUILD_ROOT
debian/rules install-po
mkdir -p ${RPM_BUILD_ROOT}/usr/lib/byobu
mkdir -p ${RPM_BUILD_ROOT}/usr/share/locale
mkdir -p ${RPM_BUILD_ROOT}/usr/share/byobu/profiles
mkdir -p ${RPM_BUILD_ROOT}/usr/share/byobu/keybindings
mkdir -p ${RPM_BUILD_ROOT}/usr/share/byobu/windows
mkdir -p ${RPM_BUILD_ROOT}/usr/bin
mkdir -p ${RPM_BUILD_ROOT}/etc/byobu
cp -ar bin/* ${RPM_BUILD_ROOT}/usr/lib/byobu
cp -ar po/locale/* ${RPM_BUILD_ROOT}/usr/share/locale
cp -ar profiles/byoburc ${RPM_BUILD_ROOT}/usr/share/byobu/profiles
cp -ar profiles/common ${RPM_BUILD_ROOT}/usr/share/byobu/profiles
cp -ar profiles/NONE ${RPM_BUILD_ROOT}/usr/share/byobu/profiles
cp -ar profiles/black ${RPM_BUILD_ROOT}/usr/share/byobu/profiles
cp -ar profiles/dark ${RPM_BUILD_ROOT}/usr/share/byobu/profiles
cp -ar profiles/light ${RPM_BUILD_ROOT}/usr/share/byobu/profiles
ln -sf f-keys ${RPM_BUILD_ROOT}/usr/share/byobu/keybindings/common
cp -ar keybindings/f-keys ${RPM_BUILD_ROOT}/usr/share/byobu/keybindings
cp -ar keybindings/none ${RPM_BUILD_ROOT}/usr/share/byobu/keybindings
cp -ar windows/common ${RPM_BUILD_ROOT}/usr/share/byobu/windows
cp -ar byobu-select-profile ${RPM_BUILD_ROOT}/usr/bin
cp -ar byobu ${RPM_BUILD_ROOT}/usr/bin
cp -ar byobu-config ${RPM_BUILD_ROOT}/usr/bin
cp -ar byobu-status ${RPM_BUILD_ROOT}/usr/bin
cp -ar byobu-status-detail ${RPM_BUILD_ROOT}/usr/bin
cp -ar byobu-launcher-install ${RPM_BUILD_ROOT}/usr/bin
cp -ar byobu-launcher-uninstall ${RPM_BUILD_ROOT}/usr/bin
cp -ar motd+shell ${RPM_BUILD_ROOT}/usr/bin
cp -ar byobu-launcher ${RPM_BUILD_ROOT}/usr/bin
cp -ar byobu-janitor ${RPM_BUILD_ROOT}/usr/bin
cp -ar byobu-export ${RPM_BUILD_ROOT}/usr/bin
cp -ar profiles/*_* ${RPM_BUILD_ROOT}/usr/share/byobu/profiles
cp -ar statusrc ${RPM_BUILD_ROOT}/etc/byobu


%clean
rm -rf $RPM_BUILD_ROOT


%files
%defattr(-,root,root,-)
/usr/bin/motd+shell
/usr/bin/byobu-launcher
/usr/bin/byobu-janitor
/usr/bin/byobu
/usr/bin/byobu-config
/usr/bin/byobu-export
/usr/bin/byobu-status
/usr/bin/byobu-status-detail
/usr/bin/byobu-select-profile
/usr/lib/byobu/arch
/usr/lib/byobu/apport
/usr/lib/byobu/battery
/usr/lib/byobu/cpu_count
/usr/lib/byobu/cpu_freq
/usr/lib/byobu/custom
/usr/lib/byobu/date
/usr/lib/byobu/disk
/usr/lib/byobu/ec2_cost
/usr/lib/byobu/fan_speed
/usr/lib/byobu/hostname
/usr/lib/byobu/ip_address
/usr/lib/byobu/load_average
/usr/lib/byobu/logo
/usr/lib/byobu/mail
/usr/lib/byobu/mem_available
/usr/lib/byobu/mem_used
/usr/lib/byobu/menu
/usr/lib/byobu/network
/usr/lib/byobu/processes
/usr/lib/byobu/reboot_required
/usr/lib/byobu/release
/usr/lib/byobu/services
/usr/lib/byobu/temp
/usr/lib/byobu/time
/usr/lib/byobu/updates_available
/usr/lib/byobu/uptime
/usr/lib/byobu/users
/usr/lib/byobu/whoami
/usr/lib/byobu/wifi_quality
/usr/share/locale/*
/usr/share/byobu/keybindings/common
/usr/share/byobu/keybindings/f-keys
/usr/share/byobu/keybindings/none
/usr/share/byobu/profiles/byoburc
/usr/share/byobu/profiles/NONE
/usr/share/byobu/profiles/black
/usr/share/byobu/profiles/common
/usr/share/byobu/profiles/dark
/usr/share/byobu/profiles/light
/usr/share/byobu/profiles/dark_blue
/usr/share/byobu/profiles/dark_cyan
/usr/share/byobu/profiles/dark_green
/usr/share/byobu/profiles/dark_purple
/usr/share/byobu/profiles/dark_red
/usr/share/byobu/profiles/dark_yellow
/usr/share/byobu/profiles/light_blue
/usr/share/byobu/profiles/light_cyan
/usr/share/byobu/profiles/light_green
/usr/share/byobu/profiles/light_purple
/usr/share/byobu/profiles/light_red
/usr/share/byobu/profiles/light_yellow
/usr/bin/byobu-launcher-install
/usr/bin/byobu-launcher-uninstall
/usr/share/byobu/windows/common
/etc/byobu
%doc README
%doc doc/help.txt
%doc debian/copyright
%doc debian/changelog
%doc COPYING


%changelog
* Fri Aug  7 2009 Derek Carter <goozbach@friocorte.com>
- Updated specfile to build on Fedora11
- Fixed some keybindings for Fedora11
- Made an expermental trigger for sourcing config on exit of byobu-config

* Tue May  5 2009 David Duffey <email@davidduffey.com>
- Initial RPM release
- see /usr/share/doc/byobu-*/changelog for upstream changelog
