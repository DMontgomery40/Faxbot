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
ENV DEBIAN_FRONTEND=noninteractive \
    EXTERNALS_CACHE_DIR=/usr/src/download-cache

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       build-essential pkg-config autoconf-archive bzip2 patch curl ca-certificates \
       libedit-dev libjansson-dev libsqlite3-dev uuid-dev libxml2-dev libssl-dev \
       libncurses-dev libspandsp-dev libtiff-dev libsrtp2-dev libunbound-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /usr/src
RUN curl --fail --location --retry 3 --output asterisk.tar.gz \
       https://downloads.asterisk.org/pub/telephony/asterisk/asterisk-22.11.0.tar.gz \
    && printf '%s\n' '3bd5ee040509a3d3cd9b1ba9520c18e6ec0a7e7981ca68c457dcd36ba3c54d94  asterisk.tar.gz' | sha256sum --check --strict \
    && tar -xzf asterisk.tar.gz \
    && mkdir -p "$EXTERNALS_CACHE_DIR" \
    && curl --fail --location --retry 3 --output "$EXTERNALS_CACHE_DIR/pjproject-2.17.tar.bz2" \
       https://raw.githubusercontent.com/asterisk/third-party/4d3b9cfdf9fe4f68d3674191707415faceec0ec6/pjproject/2.17/pjproject-2.17.tar.bz2 \
    && printf '%s\n' '04b2eb1f0f01aa0ad1945b167171843448a51aa6b7c3e806496d434f13a112b7  /usr/src/download-cache/pjproject-2.17.tar.bz2' | sha256sum --check --strict

WORKDIR /usr/src/asterisk-22.11.0
# Faxbot's own changes to Asterisk, each explained at the top of its file. Every hunk must apply
# exactly (--fuzz=0): a patch that no longer fits a new Asterisk stops the build instead of landing
# in the wrong place. faxbot_t38_gateway.h holds the spandsp steps 0002, 0003 and 0004 call (T.38
# gateway fixes, far-end frames, Internet Aware Fax), with the spandsp version guard; the replay proofs
# in tests/ compile the same file.
COPY patches/ /usr/src/patches/
RUN for change in /usr/src/patches/*.patch; do patch -p1 --forward --fuzz=0 < "$change" || exit 1; done \
    && cp /usr/src/patches/faxbot_t38_gateway.h res/
RUN ./configure --prefix=/usr --libdir=/usr/lib --sysconfdir=/etc --localstatedir=/var \
       --with-pjproject-bundled --with-spandsp \
    && make menuselect.makeopts \
    && menuselect/menuselect --disable BUILD_NATIVE \
       --disable-category MENUSELECT_ADDONS --disable-category MENUSELECT_TESTS \
       --disable-category MENUSELECT_CORE_SOUNDS --disable-category MENUSELECT_MOH \
       --disable-category MENUSELECT_EXTRA_SOUNDS \
       --enable res_fax_spandsp --enable func_base64 --enable chan_pjsip \
       --enable app_stack --enable app_userevent menuselect.makeopts \
    && menuselect/menuselect --check-deps menuselect.makeopts \
    && make -j2 \
    && make DESTDIR=/opt/install install \
    && test -f /opt/install/usr/lib/asterisk/modules/res_fax_spandsp.so \
    && test -f /opt/install/usr/lib/asterisk/modules/func_base64.so \
    && test -f /opt/install/usr/lib/asterisk/modules/chan_pjsip.so

# The engine's build, for what Faxbot learns from its calls (api/app/engine_learning.py): the Asterisk
# release and a digest of every file in patches/, so a changed patch starts that learning again. After the
# compile on purpose, so it never invalidates the cached build. start.sh copies it to Faxbot's data folder.
RUN mkdir -p /opt/install/usr/share/faxbot \
    && printf 'Asterisk 22.11.0 patches %s\n' "$(cat /usr/src/patches/* | sha256sum | cut -c1-16)" \
       > /opt/install/usr/share/faxbot/engine-version

# Debian links /var/run to /run. BuildKit will not copy a real directory over
# that link, so the installed run directory moves to where the link points,
# and the build stops if any other installed directory would land on a link.
RUN if [ -d /opt/install/var/run ] && [ ! -L /opt/install/var/run ]; then \
       mkdir -p /opt/install/run \
       && cp -a /opt/install/var/run/. /opt/install/run/ \
       && rm -rf /opt/install/var/run; \
    fi \
    && find /opt/install -mindepth 1 -type d -printf '/%P\n' | while read -r path; do \
       if [ -L "$path" ]; then echo "Installed directory $path would replace a link" >&2; exit 1; fi; \
    done

FROM base
ENV DEBIAN_FRONTEND=noninteractive LANG=C.UTF-8
LABEL org.opencontainers.image.title="Faxbot Asterisk" \
      org.opencontainers.image.version="22.11.0" \
      org.opencontainers.image.source="https://github.com/asterisk/asterisk" \
      com.faxbot.asterisk.sha256="3bd5ee040509a3d3cd9b1ba9520c18e6ec0a7e7981ca68c457dcd36ba3c54d94" \
      com.faxbot.pjproject.version="2.17" \
      com.faxbot.debian.snapshot="20261001T000000Z"

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       bash ca-certificates gettext-base dumb-init tzdata curl netcat-openbsd procps iproute2 \
       libedit2 libjansson4 libsqlite3-0 libuuid1 libxml2 libssl3t64 libncurses6 \
       libspandsp2t64 libtiff6 libsrtp2-1 libunbound8 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=build /opt/install/ /

# Asterisk parses the carrier's SIP, audio and T.38 packets, so it runs as its own unprivileged
# user (runuser and rungroup in asterisk.conf). The IDs are fixed because Faxbot's data volume
# outlives the image: the API writes each fax's pages and the inbound secret for this group.
RUN groupadd --system --gid 5060 asterisk \
    && useradd --system --uid 5060 --gid 5060 --home-dir /var/lib/asterisk --no-create-home \
       --shell /usr/sbin/nologin asterisk

WORKDIR /etc/asterisk

COPY etc/asterisk/ /etc/asterisk/
COPY start.sh /start.sh
COPY bin/faxbot-inbound-notify bin/faxbot-public-address /usr/local/bin/
# What Asterisk writes is its own; its configuration stays root's, readable by its group
# (start.sh renders the rest of it as root at every start).
RUN chmod +x /start.sh /usr/local/bin/faxbot-inbound-notify /usr/local/bin/faxbot-public-address \
    && mkdir -p /var/run/asterisk /var/log/asterisk /var/lib/asterisk /var/spool/asterisk /faxdata \
    && chown -R asterisk:asterisk /var/run/asterisk /var/log/asterisk /var/lib/asterisk /var/spool/asterisk \
    && chown -R root:asterisk /etc/asterisk && chmod -R u=rwX,g=rX,o= /etc/asterisk \
    && test -x /usr/bin/envsubst && test -x /usr/bin/ip

VOLUME ["/faxdata"]

EXPOSE 5060/udp 5060/tcp 5038 4000-4999/udp

ENTRYPOINT ["/usr/bin/dumb-init", "--"]
CMD ["/start.sh"]
