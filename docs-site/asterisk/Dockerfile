FROM debian:stable-slim

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       asterisk \
       asterisk-modules \
       curl \
       netcat-openbsd \
       procps \
       dumb-init \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /etc/asterisk

COPY etc/asterisk/ /etc/asterisk/
COPY start.sh /start.sh
RUN chmod +x /start.sh

VOLUME ["/var/spool/fax_jobs"]

EXPOSE 5060/udp 5060/tcp 5038 4000-4999/udp

ENTRYPOINT ["/usr/bin/dumb-init", "--"]
CMD ["/start.sh"]

