#!/bin/sh

error() {
	echo "ERROR: $@"
	exit 1
}

head -n1 debian/changelog | grep "unreleased" || error "This version must be 'unreleased'"

i=1
sed -i "s/) unreleased;/-0ubuntu1~ppa1) hardy;/" debian/changelog
debuild -S
sed -i "s/ppa1) hardy;/ppa2) intrepid;/" debian/changelog
debuild -S
sed -i "s/ppa2) intrepid;/ppa3) jaunty;/" debian/changelog
debuild -S

echo
echo
echo
echo "To upload:"
echo "  dput screen-profiles-ppa ../*ppa*changes"
echo
echo
echo
