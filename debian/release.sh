#!/bin/sh -e

PKG="byobu"
MAJOR=2

error() {
	echo "ERROR: $@"
	exit 1
}

head -n1 debian/changelog | grep "unreleased" || error "This version must be 'unreleased'"

./debian/rules get-orig-source
bzr bd
sed -i "s/) unreleased;/-0ubuntu1~ppa1) hardy;/" debian/changelog
bzr bd -S
sed -i "s/ppa1) hardy;/ppa2) intrepid;/" debian/changelog
bzr bd -S
sed -i "s/ppa2) intrepid;/ppa3) jaunty;/" debian/changelog
bzr bd -S
sed -i "s/~ppa3) jaunty;/) karmic;/" debian/changelog
bzr bd -S
minor=`head -n1 debian/changelog | sed "s/^.*($MAJOR.//" | sed "s/-.*$//"`
bzr tag --delete "$MAJOR.$minor" || true
bzr tag "$MAJOR.$minor"
nextminor=`expr $minor + 1`
dch -v "$MAJOR.$nextminor" "UNRELEASED"
sed -i "s/$MAJOR.$nextminor) .*;/$MAJOR.$nextminor) unreleased;/" debian/changelog
sed -i "s/^Version:.*$/Version:        $MAJOR.$nextminor/" rpm/$PKG.spec
sed -i "s%^Source0:.*$%Source0:        http://code.launchpad.net/$PKG/trunk/$MAJOR.$nextminor/+download/byobu_$MAJOR.$nextminor.orig.tar.gz%" rpm/$PKG.spec

gpg --armor --sign --detach-sig ../"$PKG"_*.orig.tar.gz

sudo alien --to-rpm ../$PKG"_"$MAJOR.$minor"_all.deb"
mv -f *.rpm ..
rsync -aP ../*rpm kirkland@people.ubuntu.com:~kirkland/public_html/$PKG/rpm

$PKG-export -c light -f /tmp/$PKG-export.tar.gz
rsync -aP /tmp/$PKG-export.tar.gz kirkland@people.ubuntu.com:~kirkland/public_html

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
echo "  bzr commit -m 'releasing $MAJOR.$minor, opening $MAJOR.$nextminor' && bzr push lp:$PKG"
echo
echo "Publish tarball at:"
echo "  https://launchpad.net/$PKG/trunk/+addrelease"
echo
echo
