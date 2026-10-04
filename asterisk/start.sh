#!/usr/bin/env bash
set -euo pipefail

# No real trunk or AMI account is created unless explicitly configured.
umask 077
# The directories are fixed in the image; tests point them elsewhere.
tpl_dir=${FAXBOT_ASTERISK_TEMPLATES:-/etc/asterisk/templates}
out_dir=${FAXBOT_ASTERISK_ETC:-/etc/asterisk}
data_dir=${FAXBOT_DATA:-/faxdata}
# Shared with Faxbot: it writes the trunk and secrets here, and reads what Asterisk loaded.
shared=$data_dir/asterisk
mkdir -p "$out_dir" "$shared"

refuse() { printf '%s\n' "$1" >&2; exit 1; }
safe_value() {
  local forbidden='[[:cntrl:];\\]'
  [[ -n "$1" && ! "$1" =~ $forbidden \
    && ! "$1" =~ ^[[:space:]] && ! "$1" =~ [[:space:]]$ ]]
}
render() {
  local name=$1 variables=$2 temporary
  temporary=$(mktemp "$out_dir/.${name}.XXXXXX")
  envsubst "$variables" < "$tpl_dir/${name}.template" > "$temporary"
  mv -f "$temporary" "$out_dir/$name"
}

if [ -n "${ASTERISK_AMI_USERNAME:-}${ASTERISK_AMI_PASSWORD:-}" ]; then
  [ -n "${ASTERISK_AMI_USERNAME:-}" ] && [ -n "${ASTERISK_AMI_PASSWORD:-}" ] \
    || refuse 'Incomplete AMI configuration'
  [[ "$ASTERISK_AMI_USERNAME" =~ ^[A-Za-z0-9_-]{1,64}$ ]] \
    && [[ ! "$ASTERISK_AMI_USERNAME" =~ ^[Gg][Ee][Nn][Ee][Rr][Aa][Ll]$ ]] \
    && safe_value "$ASTERISK_AMI_PASSWORD" || refuse 'Unsupported AMI configuration syntax'
  render manager.conf '${ASTERISK_AMI_USERNAME} ${ASTERISK_AMI_PASSWORD}'
else
  printf '%s\n' '[general]' 'enabled=no' 'webenabled=no' > "$out_dir/manager.conf"
fi

# A public-address install publishes one narrow media range
# (docker-compose.public.yml), and Asterisk must use exactly that range: the
# first third for T.38 (UDPTL), the rest for audio (RTP and RTCP).
if [ -n "${FAXBOT_MEDIA_PORTS:-}" ]; then
  [[ "$FAXBOT_MEDIA_PORTS" =~ ^([0-9]{1,5})-([0-9]{1,5})$ ]] || refuse 'Unsupported media port range'
  first=$((10#${BASH_REMATCH[1]})) last=$((10#${BASH_REMATCH[2]}))
  (( first >= 1024 && last <= 65535 && last - first >= 5 && last - first < 2000 )) \
    || refuse 'Unsupported media port range'
  udptl_last=$(( first + (last - first + 1) / 3 - 1 ))
  sed -i -e "s/^udptlstart=.*/udptlstart=$first/" -e "s/^udptlend=.*/udptlend=$udptl_last/" "$out_dir/udptl.conf"
  sed -i -e "s/^rtpstart=.*/rtpstart=$((udptl_last + 1))/" -e "s/^rtpend=.*/rtpend=$last/" "$out_dir/rtp.conf"
fi

# Faxbot writes this file from its SIP trunk settings (console "Apply to
# Asterisk" or "python -m app.sip_trunk write"); it replaces the older
# SIP_USERNAME/SIP_PASSWORD/SIP_SERVER settings when present.
trunk_conf=${FAXBOT_TRUNK_CONF:-$shared/pjsip.conf}
mkdir -p "$data_dir/inbound"
rm -f "$shared/pjsip.conf.started"
if [ -f "$trunk_conf" ] && [ ! -L "$trunk_conf" ]; then
  temporary=$(mktemp "$out_dir/.pjsip.conf.XXXXXX")
  cat "$trunk_conf" > "$temporary"
  # The trunk exactly as loaded now, before addresses are filled in; Faxbot
  # compares it with its settings to tell whether Asterisk needs a restart.
  started=$(mktemp "$shared/.pjsip.conf.started.XXXXXX")
  cat "$temporary" > "$started"
  mv -f "$started" "$shared/pjsip.conf.started"
  mv -f "$temporary" "$out_dir/pjsip.conf"
  # The internet address Faxbot found, or no address lines at all (no network call here).
  "${FAXBOT_PUBLIC_ADDRESS_BIN:-/usr/local/bin/faxbot-public-address}" "$out_dir/pjsip.conf"
elif [ -n "${SIP_USERNAME:-}${SIP_PASSWORD:-}${SIP_SERVER:-}" ]; then
  [ -n "${SIP_USERNAME:-}" ] && [ -n "${SIP_PASSWORD:-}" ] && [ -n "${SIP_SERVER:-}" ] \
    || refuse 'Incomplete SIP configuration'
  [[ "$SIP_USERNAME" =~ ^[A-Za-z0-9_.+@-]+$ ]] && safe_value "$SIP_PASSWORD" \
    && [[ "$SIP_SERVER" =~ ^[A-Za-z0-9_.-]+(:[0-9]{1,5})?$ ]] \
    || refuse 'Unsupported SIP configuration syntax'
  export SIP_FROM_DOMAIN="${SIP_FROM_DOMAIN:-${SIP_SERVER%%:*}}"
  [[ "$SIP_FROM_DOMAIN" =~ ^[A-Za-z0-9_.-]+$ ]] || refuse 'Unsupported SIP configuration syntax'
  render pjsip.conf '${SIP_USERNAME} ${SIP_PASSWORD} ${SIP_SERVER} ${SIP_FROM_DOMAIN}'
  case "${SIP_REGISTER:-false}" in
    true) cat >> "$out_dir/pjsip.conf" <<EOF

[trunk-registration]
type=registration
outbound_auth=trunk-auth
server_uri=sip:${SIP_SERVER}
client_uri=sip:${SIP_USERNAME}@${SIP_SERVER}
contact_user=${SIP_USERNAME}
retry_interval=60
forbidden_retry_interval=600
expiration=300
transport=transport-udp
endpoint=trunk-endpoint
EOF
      ;;
    false) ;;
    *) refuse 'Unsupported SIP registration setting' ;;
  esac
else
  printf '%s\n' '[global]' 'type=global' 'user_agent=Faxbot-Asterisk' \
    '[transport-udp]' 'type=transport' 'protocol=udp' 'bind=0.0.0.0' > "$out_dir/pjsip.conf"
fi

# This Asterisk shares Faxbot's data folder, so Faxbot may restart it (over the
# manager connection, once no call is up) to load new settings; Docker's
# restart policy starts it again.
date +%s > "$shared/engine-started"

if [ -n "${FAXBOT_ASTERISK_COMMAND:-}" ]; then
  exec "$FAXBOT_ASTERISK_COMMAND"
fi
exec asterisk -f -C "$out_dir/asterisk.conf"
