#!/bin/sh
# Send one test fax from the SR140 stand-in, the way RightFax's SR140 hands a fax to its Primary Gateway.
#
#   ./send.sh NUMBER [ENDPOINT] [TIFF]
#
# NUMBER    the number as RightFax would dial it (Faxbot removes the server's outside-line prefix, if you set one)
# ENDPOINT  faxbot-gateway (the right password, the default), faxbot-gateway-wrong (a wrong password) or
#           faxbot-gateway-none (no password): the last two must be refused
# TIFF      a fax TIFF inside ./fax (default fax/test-fax.tiff)
#
# Waits for the call to end and prints the stand-in's line for it from fax/results.log, then the SIP answers it got.
set -eu
here=$(cd "$(dirname "$0")" && pwd)
number=$(printf '%s' "${1:?Give the number to dial, such as 13035550199}" | tr -cd '0-9+*#')
endpoint=${2:-faxbot-gateway}
case $endpoint in faxbot-gateway|faxbot-gateway-wrong|faxbot-gateway-none) ;; *) echo 'Unknown endpoint' >&2; exit 2 ;; esac
tiff=$(basename "${3:-test-fax.tiff}")
compose="docker compose -f $here/docker-compose.yml"
before=$(wc -l < "$here/fax/results.log" 2>/dev/null || echo 0)
$compose exec -T sr140 asterisk -C /tmp/sr140/etc/asterisk.conf -rx 'pjsip set history clear' >/dev/null
$compose exec -T sr140 asterisk -C /tmp/sr140/etc/asterisk.conf -rx 'pjsip set history on' >/dev/null
# A call file, written aside and moved in, so Asterisk never reads half of it.
$compose exec -T sr140 sh -c 'cat > /tmp/sr140/spool/next.call && mv /tmp/sr140/spool/next.call /tmp/sr140/spool/outgoing/' <<EOF
Channel: PJSIP/${number}@${endpoint}
CallerID: "SR140 stand-in" <15555550100>
MaxRetries: 0
WaitTime: 60
Context: sr140-send
Extension: s
Priority: 1
Setvar: FAXFILE=/fax/${tiff}
Setvar: SR140_TO=${number}
Archive: yes
EOF
printf 'Calling %s through %s ...\n' "$number" "$endpoint"
waited=0
while [ "$(wc -l < "$here/fax/results.log" 2>/dev/null || echo 0)" -le "$before" ]; do
  waited=$((waited + 2))
  if [ "$waited" -gt 600 ]; then echo 'No result after 10 minutes.' >&2; exit 1; fi
  sleep 2
done
tail -n +"$((before + 1))" "$here/fax/results.log"
$compose exec -T sr140 asterisk -C /tmp/sr140/etc/asterisk.conf -rx 'pjsip show history' | grep -E 'SIP/2.0 [0-9]{3}' || true
