#!/bin/sh -e

PKG="byobu"

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
curver=`head -n1 debian/changelog | sed "s/^.*(2.//" | sed "s/-.*$//"`
bzr tag --delete 2.$curver || true
bzr tag 2.$curver
ver=`expr $curver + 1`
dch -v "2.$ver" "UNRELEASED"
sed -i "s/2.$ver) .*;/2.$ver) unreleased;/" debian/changelog
sed -i "s/^Version:.*$/Version:        2.$ver/" rpm/$PKG.spec
sed -i "s%^Source0:.*$%Source0:        http://code.launchpad.net/$PKG/trunk/2.$ver/+download/byobu_2.$ver.orig.tar.gz%" rpm/$PKG.spec

gpg --armor --sign --detach-sig ../"$PKG"_*.orig.tar.gz

$PKG-export -c light -f /tmp/$PKG-export.tar.gz
puc /tmp/$PKG-export.tar.gz

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
echo "  bzr commit -m 'releasing 2.$curver, opening 2.$ver' && bzr push lp:$PKG"
echo
echo "Publish tarball at:"
echo "  https://launchpad.net/$PKG/trunk/+addrelease"
echo
echo
