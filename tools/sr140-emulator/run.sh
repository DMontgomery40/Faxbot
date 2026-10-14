#!/bin/sh
# Start the SR140 stand-in's Asterisk (inside its container; docker-compose.yml runs this). It needs:
#   GATEWAY          Faxbot's address on your network (FAXBOT_LAN_ADDRESS on the Faxbot host)
#   GATEWAY_PORT     the port Faxbot publishes for fax servers there (FAXBOT_FAX_SERVER_PORT, default 5060)
#   SR140_USERNAME   the user name Faxbot showed when you added this fax server
#   SR140_PASSWORD   the password Faxbot showed once
#   SR140_PORT       the stand-in's own SIP port (default 5090)
set -eu
: "${GATEWAY:?Set GATEWAY to the address of Faxbot on your network}"
: "${SR140_USERNAME:?Set SR140_USERNAME to the user name Faxbot showed}"
: "${SR140_PASSWORD:?Set SR140_PASSWORD to the password Faxbot showed}"
GATEWAY_PORT=${GATEWAY_PORT:-5060}
SR140_PORT=${SR140_PORT:-5090}
export GATEWAY GATEWAY_PORT SR140_USERNAME SR140_PASSWORD SR140_PORT
umask 022
mkdir -p /tmp/sr140/etc /tmp/sr140/lib /tmp/sr140/spool/outgoing /tmp/sr140/spool/outgoing_done /tmp/sr140/run \
  /tmp/sr140/log /fax
for file in /emulator/etc/*.conf; do cp "$file" /tmp/sr140/etc/; done
envsubst '${GATEWAY} ${GATEWAY_PORT} ${SR140_USERNAME} ${SR140_PASSWORD} ${SR140_PORT}' \
  < /emulator/etc/pjsip.conf.template > /tmp/sr140/etc/pjsip.conf
chmod 600 /tmp/sr140/etc/pjsip.conf
printf 'SR140 stand-in: calls go to %s port %s as %s\n' "$GATEWAY" "$GATEWAY_PORT" "$SR140_USERNAME"
exec asterisk -f -C /tmp/sr140/etc/asterisk.conf
