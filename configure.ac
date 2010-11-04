#                                               -*- Autoconf -*-
# Process this file with autoconf to produce a configure script.

AC_PREREQ([2.61])
AC_INIT(byobu, 3.0, http://bugs.launchpad.net/byobu)
AM_INIT_AUTOMAKE(byobu, 3.0)

# Checks for programs.

# Checks for libraries.

# Checks for header files.

# Checks for typedefs, structures, and compiler characteristics.

# Checks for library functions.

AC_OUTPUT(Makefile \
	etc/byobu/Makefile \
	etc/update-motd.d/Makefile \
	usr/share/applications/Makefile \
	usr/share/byobu/ec2/Makefile \
	usr/share/byobu/keybindings/Makefile \
	usr/share/byobu/pixmaps/Makefile \
	usr/share/byobu/profiles/Makefile \
	usr/share/byobu/tests/Makefile \
	usr/share/byobu/windows/Makefile \
	usr/share/doc/byobu/Makefile \
	usr/lib/byobu/Makefile \
	usr/share/man/man1/Makefile usr/bin/Makefile)

if test -n "${prefix}" && test "${prefix}" != "/usr"; then
	for i in usr/bin/byobu*; do
		# This hackery seems necessary for Mac OSX and Solaris installs
		sed -e "s:BYOBU_PREFIX=.*:BYOBU_PREFIX=\"${prefix}\":" "$i" > "$i.new"
		mv -f "$i.new" "$i"
		chmod 755 "$i"
	done
fi
