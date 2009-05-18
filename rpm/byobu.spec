Name:           byobu
Version:        2.5
Release:        1%{?dist}
Summary:        a set of useful profiles and a profile-switcher for GNU screen

Group:          Applications/System
License:        GPL
URL:            http://launchpad.net/byobu
Source0:        http://code.launchpad.net/byobu/trunk/2.5/+download/byobu_2.5.orig.tar.gz
BuildRoot:      %{_tmppath}/%{name}-%{version}-%{release}-root-%(%{__id_u} -n)
BuildArch:	noarch

BuildRequires:  gettext
Requires:       screen, python >= 2.5, newt, gettext

# TODO
#   help.txt is in different locations under RPM/DEB
#   python 2.5 is not available on RHEL 5

%package extras
Summary:        a set of useful profiles and a profile-switcher for GNU screen
Group:          Applications/System
Requires:	byobu

%description
byobu includes a set of profiles for the GNU screen window manager.
These profiles are quite useful on server machines which are not running
a graphical desktop.  The 'screen' command provides a number of advanced
features are not necessarily exposed in the default profile.  These profiles
provide features such as status bars, clocks, notifiers (reboot-required,
updates-available), etc.  The profile-switcher allows users to quickly switch
their .screenrc to any of the available profiles.

update-notifier-common provides a more efficient and standard mechanism for
calculating the number of updates available in the status panel.


%description extras
The byobu package contains a basic set of light and dark profiles.
The byobu-extras package provides additional profiles of various
different light and dark colors.


%prep
%setup -q -n %{name}_%{version}.orig


%build
profiles/generate


%install
rm -rf $RPM_BUILD_ROOT
debian/rules install-po
mkdir -p ${RPM_BUILD_ROOT}/usr/lib/byobu
mkdir -p ${RPM_BUILD_ROOT}/usr/share/locale
mkdir -p ${RPM_BUILD_ROOT}/usr/share/byobu/profiles
mkdir -p ${RPM_BUILD_ROOT}/usr/share/byobu/keybindings
mkdir -p ${RPM_BUILD_ROOT}/usr/share/byobu/windows
mkdir -p ${RPM_BUILD_ROOT}/usr/bin
cp -ar bin/* ${RPM_BUILD_ROOT}/usr/lib/byobu
cp -ar po/locale/* ${RPM_BUILD_ROOT}/usr/share/locale
cp -ar profiles/byoburc ${RPM_BUILD_ROOT}/usr/share/byobu/profiles
cp -ar profiles/common ${RPM_BUILD_ROOT}/usr/share/byobu/profiles
cp -ar profiles/NONE ${RPM_BUILD_ROOT}/usr/share/byobu/profiles
cp -ar profiles/black ${RPM_BUILD_ROOT}/usr/share/byobu/profiles
cp -ar profiles/dark ${RPM_BUILD_ROOT}/usr/share/byobu/profiles
cp -ar profiles/light ${RPM_BUILD_ROOT}/usr/share/byobu/profiles
cp -ar keybindings/common ${RPM_BUILD_ROOT}/usr/share/byobu/keybindings
cp -ar keybindings/none ${RPM_BUILD_ROOT}/usr/share/byobu/keybindings
cp -ar windows/common ${RPM_BUILD_ROOT}/usr/share/byobu/windows
cp -ar select-screen-profile ${RPM_BUILD_ROOT}/usr/bin
cp -ar byobu ${RPM_BUILD_ROOT}/usr/bin
cp -ar byobu-config ${RPM_BUILD_ROOT}/usr/bin
cp -ar byobu-status ${RPM_BUILD_ROOT}/usr/bin
cp -ar byobu-status-detail ${RPM_BUILD_ROOT}/usr/bin
cp -ar screen-launcher-install ${RPM_BUILD_ROOT}/usr/share/byobu
cp -ar screen-launcher-uninstall ${RPM_BUILD_ROOT}/usr/share/byobu
cp -ar motd+shell ${RPM_BUILD_ROOT}/usr/bin
cp -ar screen-launcher ${RPM_BUILD_ROOT}/usr/bin
cp -ar byobu-export ${RPM_BUILD_ROOT}/usr/bin
cp -ar profiles/*_* ${RPM_BUILD_ROOT}/usr/share/byobu/profiles


%clean
rm -rf $RPM_BUILD_ROOT


%files
%defattr(-,root,root,-)
/usr/bin/motd+shell
/usr/bin/screen-launcher
/usr/bin/byobu
/usr/bin/byobu-config
/usr/bin/byobu-export
/usr/bin/byobu-status
/usr/bin/byobu-status-detail
/usr/bin/select-screen-profile
/usr/lib/byobu/arch
/usr/lib/byobu/battery
/usr/lib/byobu/cpu-count
/usr/lib/byobu/cpu-freq
/usr/lib/byobu/date
/usr/lib/byobu/disk-available
/usr/lib/byobu/disk-used
/usr/lib/byobu/ec2-cost
/usr/lib/byobu/hostname
/usr/lib/byobu/ip-address
/usr/lib/byobu/load-average
/usr/lib/byobu/logo
/usr/lib/byobu/mem-available
/usr/lib/byobu/mem-used
/usr/lib/byobu/menu
/usr/lib/byobu/network-down
/usr/lib/byobu/network-up
/usr/lib/byobu/processes
/usr/lib/byobu/reboot-required
/usr/lib/byobu/release
/usr/lib/byobu/time
/usr/lib/byobu/updates-available
/usr/lib/byobu/uptime
/usr/lib/byobu/users
/usr/lib/byobu/whoami
/usr/lib/byobu/wifi-quality
/usr/lib/byobu/disk-available
/usr/lib/byobu/disk-used
/usr/share/locale/es/LC_MESSAGES/byobu.mo
/usr/share/locale/fr/LC_MESSAGES/byobu.mo
/usr/share/byobu/keybindings/common
/usr/share/byobu/keybindings/none
/usr/share/byobu/profiles/byoburc
/usr/share/byobu/profiles/NONE
/usr/share/byobu/profiles/black
/usr/share/byobu/profiles/common
/usr/share/byobu/profiles/dark
/usr/share/byobu/profiles/light
/usr/share/byobu/screen-launcher-install
/usr/share/byobu/screen-launcher-uninstall
/usr/share/byobu/windows/common
%doc README
%doc doc/help.txt
%doc debian/copyright
%doc debian/changelog
%doc COPYING


%files extras
%defattr(-,root,root,-)
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


%changelog
* Tue May  5 2009 David Duffey <email@davidduffey.com>
- Initial RPM release
- see /usr/share/doc/byobu-*/changelog for upstream changelog
