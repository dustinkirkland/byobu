#!/bin/sh -e

PKG="screen-profiles"

error() {
	echo "ERROR: $@"
	exit 1
}

head -n1 debian/changelog | grep "unreleased" || error "This version must be 'unreleased'"

i=1
bzr bd
sed -i "s/) unreleased;/-0ubuntu1~ppa1) hardy;/" debian/changelog
bzr bd -S
sed -i "s/ppa1) hardy;/ppa2) intrepid;/" debian/changelog
bzr bd -S
sed -i "s/ppa2) intrepid;/ppa3) jaunty;/" debian/changelog
bzr bd -S
sed -i "s/~ppa3) jaunty;/) karmic;/" debian/changelog
bzr bd -S
curver=`head -n1 debian/changelog | sed "s/^.*(1.//" | sed "s/-.*$//"`
bzr tag --delete 1.$curver || true
bzr tag 1.$curver
ver=`expr $curver + 1`
dch -v "1.$ver" "UNRELEASED"
sed -i "s/1.$ver) karmic;/1.$ver) unreleased;/" debian/changelog

gpg --armor --sign --detach-sig ../"$PKG"_*.orig.tar.gz

echo
echo
echo "To test:"
echo "  sudo dpkg -i ../*.deb"
echo
echo "To upload PPA packages:"
echo "  dput $PKG-ppa ../*ppa*changes"
echo
echo "To commit and push:"
echo "  bzr cdiff"
echo "  bzr commit -m "releasing $curver, opening $ver" && bzr push"
echo
echo "Publish tarball at:"
echo "  https://launchpad.net/$PKG/trunk/+addrelease"
echo
echo
