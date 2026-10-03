#!/usr/bin/env bash
set -euo pipefail

# No real trunk or AMI account is created unless explicitly configured.
umask 077
tpl_dir=/etc/asterisk/templates
out_dir=/etc/asterisk
mkdir -p "$out_dir"

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
    && [[ "${ASTERISK_AMI_USERNAME,,}" != general ]] \
    && safe_value "$ASTERISK_AMI_PASSWORD" || refuse 'Unsupported AMI configuration syntax'
  render manager.conf '${ASTERISK_AMI_USERNAME} ${ASTERISK_AMI_PASSWORD}'
else
  printf '%s\n' '[general]' 'enabled=no' 'webenabled=no' > "$out_dir/manager.conf"
fi

# Faxbot writes this file from its SIP trunk settings (console "Apply to
# Asterisk" or "python -m app.sip_trunk write"); it replaces the older
# SIP_USERNAME/SIP_PASSWORD/SIP_SERVER settings when present.
trunk_conf=${FAXBOT_TRUNK_CONF:-/faxdata/asterisk/pjsip.conf}
mkdir -p /faxdata/inbound
if [ -f "$trunk_conf" ] && [ ! -L "$trunk_conf" ]; then
  temporary=$(mktemp "$out_dir/.pjsip.conf.XXXXXX")
  cat "$trunk_conf" > "$temporary"
  mv -f "$temporary" "$out_dir/pjsip.conf"
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

exec asterisk -f -C "$out_dir/asterisk.conf"
