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
       bash ca-certificates gettext-base dumb-init tzdata curl netcat-openbsd procps \
       libedit2 libjansson4 libsqlite3-0 libuuid1 libxml2 libssl3t64 libncurses6 \
       libspandsp2t64 libtiff6 libsrtp2-1 libunbound8 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=build /opt/install/ /

WORKDIR /etc/asterisk

COPY etc/asterisk/ /etc/asterisk/
COPY start.sh /start.sh
RUN chmod +x /start.sh \
    && mkdir -p /var/run/asterisk /var/log/asterisk /var/lib/asterisk /var/spool/asterisk /faxdata \
    && test -x /usr/bin/envsubst

VOLUME ["/faxdata"]

EXPOSE 5060/udp 5060/tcp 5038 4000-4999/udp

ENTRYPOINT ["/usr/bin/dumb-init", "--"]
CMD ["/start.sh"]
