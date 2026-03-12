COMMENT =	text-based window manager and terminal multiplexer

V =		6.14
DISTNAME =	byobu-${V}
CATEGORIES =	sysutils

HOMEPAGE =	https://www.byobu.org/
MAINTAINER =	Corey Leavitt <corey@leavitt.info>

# GPLv3
PERMIT_PACKAGE =	Yes

MASTER_SITES =	https://github.com/coreyleavitt/byobu/releases/download/${V}/
DISTFILES =	byobu-${V}.tar.xz

WRKDIST =	${WRKDIR}/byobu-${V}

RUN_DEPENDS =	shells/bash \
		misc/tmux

CONFIGURE_STYLE =	gnu
CONFIGURE_ARGS =	--prefix=${PREFIX}

USE_GMAKE =		No

.include <bsd.port.mk>
