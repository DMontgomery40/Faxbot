# Faxbot's fax engine for SSL Fax: HylaFAX+ 7.0.11 with IAXmodem lines that
# register with Faxbot's own Asterisk. See docs/setup/sslfax.md.
#
# The first stage is byte-for-byte the base stage of asterisk/Dockerfile (same
# Debian image digest and package snapshot), so both images share it.
FROM debian:trixie-slim@sha256:a99cfc517144bc59b1978475ec53b46ecabec7e43635402ee5b77cc54cd1b20a AS base

# Freeze authenticated Debian package metadata as well as the base image.
# Snapshot expiry is intentionally ignored; package signatures remain required.
RUN rm -f /etc/apt/sources.list /etc/apt/sources.list.d/debian.sources \
    && printf '%s\n' \
       'deb [check-valid-until=no] http://snapshot.debian.org/archive/debian/20261001T000000Z/ trixie main' \
       'deb [check-valid-until=no] http://snapshot.debian.org/archive/debian/20261001T000000Z/ trixie-updates main' \
       'deb [check-valid-until=no] http://snapshot.debian.org/archive/debian-security/20261001T000000Z/ trixie-security main' \
       > /etc/apt/sources.list.d/faxbot-snapshot.list

FROM base AS build
ENV DEBIAN_FRONTEND=noninteractive

# configure records Ghostscript's path; HylaFAX's image preparation uses it
# for any page it cannot send as it is. libjbig-dev (jbigkit 2.1) gives
# HylaFAX+ its JBIG (T.85) coding: configure defines HAVE_JBIG when it finds
# jbg_enc_init, and faxsend then sends JBIG to machines whose DIS offers it
# (faxd/MemoryDecoder.c++). libtiff-dev pulled it in by itself until now; it is
# named here so the build stops if it ever goes missing.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       g++ make patch curl ca-certificates gawk sed file \
       libtiff-dev libtiff-tools zlib1g-dev libssl-dev libjpeg62-turbo-dev libjbig-dev ghostscript \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /usr/src
RUN curl --fail --location --retry 3 --output hylafax.tar.gz \
       https://downloads.sourceforge.net/project/hylafax/hylafax/hylafax-7.0.11.tar.gz \
    && printf '%s\n' 'd696df653b1c59664fddd2c703edc77ed42766a16b2ce19c958933d76fd62287  hylafax.tar.gz' \
       | sha256sum --check --strict \
    && tar -xzf hylafax.tar.gz

# Faxbot's changes, applied in file order (0001, 0002, ...): the SSL Fax client
# refuses private, loopback, link-local, metadata, multicast and reserved
# addresses (except this engine's own listener), keeps the session passcode out
# of logs, and lets the listener bind again right after a call (0001); and the
# engine can be polled: a document held in pollq for a caller is offered in its
# DIS and sent when the caller answers DTC (0002, T.30 role reversal). Each
# patch's rules are tested here; a failure stops the build, and so does a hunk
# that does not apply exactly (--fuzz=0).
COPY patches/ /usr/src/patches/
COPY tests/sslfax_policy_test.c++ tests/polled_transmit_test.c++ /usr/src/tests/
WORKDIR /usr/src/hylafax-7.0.11
RUN for patch in /usr/src/patches/*.patch; do patch -p1 --forward --batch --fuzz=0 < "$patch" || exit 1; done \
    && g++ -Wall -Werror -I faxd -o /usr/src/sslfax-policy-test /usr/src/tests/sslfax_policy_test.c++ \
    && /usr/src/sslfax-policy-test \
    && g++ -Wall -Werror -I faxd -o /usr/src/polled-transmit-test /usr/src/tests/polled_transmit_test.c++ \
    && /usr/src/polled-transmit-test

# Serial make: parallel builds race on libfaxutil.
RUN ./configure --nointeractive --disable-pam \
       --with-DIR_BIN=/usr/bin --with-DIR_SBIN=/usr/sbin --with-DIR_LIB=/usr/lib \
       --with-DIR_LIBDATA=/etc/hylafax --with-DIR_LIBEXEC=/usr/sbin \
       --with-DIR_LOCKS=/var/lock --with-DIR_MAN=/usr/share/man \
       --with-DIR_SPOOL=/var/spool/hylafax --with-PATH_GSRIP=/usr/bin/gs \
       --with-AFM=no --with-HTML=no --with-OPTIMIZER="-O2" --with-DSO=auto \
       --with-TIFFBIN=/usr/bin --with-SENDMAIL=/bin/true \
       --with-PATH_VGETTY=/bin/vgetty --with-PATH_GETTY=/sbin/getty --with-PATH_EGETTY=/bin/egetty \
    && grep -q '^#define HAVE_SSL 1' config.h \
    && grep -q '^#define HAVE_JBIG 1' config.h \
    && make \
    && make install INSTALLROOT=/opt/install \
    && test -x /opt/install/usr/sbin/faxgetty && test -x /opt/install/usr/sbin/faxq \
    && test -x /opt/install/usr/sbin/hfaxd && test -d /opt/install/var/spool/hylafax/bin \
    && rm -rf /opt/install/usr/share/man

# The engine's build, for what Faxbot learns from its calls (api/app/engine_learning.py): the HylaFAX+
# release and a digest of every file in patches/, so a changed patch starts that learning again. The
# engine reports it in its status file.
RUN mkdir -p /opt/install/usr/share/faxbot \
    && printf 'HylaFAX+ 7.0.11 patches %s\n' "$(cat /usr/src/patches/* | sha256sum | cut -c1-16)" \
       > /opt/install/usr/share/faxbot/engine-version

FROM base
ENV DEBIAN_FRONTEND=noninteractive LANG=C.UTF-8
LABEL org.opencontainers.image.title="Faxbot fax engine (HylaFAX+)" \
      org.opencontainers.image.version="7.0.11" \
      org.opencontainers.image.source="https://hylafax.sourceforge.io/" \
      com.faxbot.hylafax.sha256="d696df653b1c59664fddd2c703edc77ed42766a16b2ce19c958933d76fd62287" \
      com.faxbot.debian.snapshot="20261001T000000Z"

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       bash gawk sed procps psmisc file dumb-init curl ca-certificates openssl busybox \
       libtiff6 libtiff-tools libjbig0 libjpeg62-turbo zlib1g libssl3t64 libstdc++6 iaxmodem ghostscript \
    && rm -rf /var/lib/apt/lists/*

COPY --from=build /opt/install/ /
COPY LICENSE LICENSE-MIT /usr/share/doc/faxbot/

# faxsetup writes etc/setup.cache with the tool paths of this stage.
RUN mkdir -p /run/lock && chmod 1777 /run/lock \
    && faxsetup -server -nointeractive -quiet </dev/null >/tmp/faxsetup.log 2>&1 \
    || { tail -20 /tmp/faxsetup.log; exit 1; }
RUN test -f /var/spool/hylafax/etc/setup.cache \
    && ! ldd /usr/sbin/faxgetty /usr/sbin/faxq /usr/sbin/faxsend /usr/sbin/hfaxd /usr/bin/iaxmodem | grep -q 'not found' \
    && rm -f /tmp/faxsetup.log /var/spool/hylafax/etc/ssl.pem

COPY entrypoint.sh /usr/local/sbin/faxbot-engine
COPY bin/ /usr/local/lib/faxbot-engine/
RUN chmod 755 /usr/local/sbin/faxbot-engine /usr/local/lib/faxbot-engine/* \
    && mkdir -p /var/lib/faxbot-engine /faxdata

# libjbig0 (jbigkit 2.1, GPL-2.0-or-later) is linked into HylaFAX+'s fax server library for JBIG; HylaFAX+'s
# own licence (COPYRIGHT: SGI/Leffler, permissive) allows that combination. This file installs Debian's
# binary packages; anyone who distributes a built image must also offer the GPL source (the matching Debian
# source packages, `apt-get source jbigkit iaxmodem`) with it or in writing for three years.
RUN ldd /usr/lib/libfaxserver.so.7.0.11 | grep -q 'libjbig.so'

# Port 4559 (job submission) stays on the private Compose network; the SSL Fax
# listener is published only by docker-compose.sslfax.yml.
VOLUME ["/var/lib/faxbot-engine"]
ENTRYPOINT ["/usr/bin/dumb-init", "--"]
CMD ["/usr/local/sbin/faxbot-engine"]
