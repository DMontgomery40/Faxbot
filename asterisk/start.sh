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
# The SSL Fax engine's settings folder (read-only for the engine, which never sees
# the folder above): which fax lines this Asterisk loaded, so the engine registers
# them only once they exist.
engine_dir=$data_dir/hylafax
mkdir -p "$out_dir" "$shared" "$engine_dir"

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
# sed over a file through a temporary copy (GNU and BSD sed differ on -i).
edit() {
  local file=$1 temporary
  shift
  temporary=$(mktemp "$file.XXXXXX")
  sed "$@" "$file" > "$temporary"
  mv -f "$temporary" "$file"
}
octet='(25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9]?[0-9])'

# The manager login Faxbot uses. ASTERISK_AMI_USERNAME and ASTERISK_AMI_PASSWORD
# in the environment win (the API reads the same .env). Otherwise Faxbot writes
# the login it created to the shared folder when the SIP trunk first comes into
# use; until then the manager port stays off.
credentials=$shared/manager.credentials
check_login() {
  [[ "$ASTERISK_AMI_USERNAME" =~ ^[A-Za-z0-9_-]{1,64}$ ]] \
    && [[ ! "$ASTERISK_AMI_USERNAME" =~ ^[Gg][Ee][Nn][Ee][Rr][Aa][Ll]$ ]] \
    && safe_value "$ASTERISK_AMI_PASSWORD" || refuse 'Unsupported AMI configuration syntax'
}
login_from=none
if [ -n "${ASTERISK_AMI_USERNAME:-}${ASTERISK_AMI_PASSWORD:-}" ]; then
  [ -n "${ASTERISK_AMI_USERNAME:-}" ] && [ -n "${ASTERISK_AMI_PASSWORD:-}" ] \
    || refuse 'Incomplete AMI configuration'
  check_login
  login_from=environment
elif [ -f "$credentials" ] && [ ! -L "$credentials" ]; then
  ASTERISK_AMI_USERNAME='' ASTERISK_AMI_PASSWORD=''
  { IFS= read -r ASTERISK_AMI_USERNAME || true; IFS= read -r ASTERISK_AMI_PASSWORD || true; } < "$credentials"
  check_login
  export ASTERISK_AMI_USERNAME ASTERISK_AMI_PASSWORD
  login_from=faxbot
fi
if [ "$login_from" = none ]; then
  printf '%s\n' '[general]' 'enabled=no' 'webenabled=no' > "$out_dir/manager.conf"
else
  render manager.conf '${ASTERISK_AMI_USERNAME} ${ASTERISK_AMI_PASSWORD}'
fi

# A public-address install (docker-compose.public.yml) or a phone system
# install (docker-compose.phone-system.yml) publishes one narrow media range,
# and Asterisk must use exactly that range: the first third for T.38 (UDPTL),
# the rest for audio (RTP and RTCP).
if [ -n "${FAXBOT_MEDIA_PORTS:-}" ]; then
  [[ "$FAXBOT_MEDIA_PORTS" =~ ^([0-9]{1,5})-([0-9]{1,5})$ ]] || refuse 'Unsupported media port range'
  first=$((10#${BASH_REMATCH[1]})) last=$((10#${BASH_REMATCH[2]}))
  (( first >= 1024 && last <= 65535 && last - first >= 5 && last - first < 2000 )) \
    || refuse 'Unsupported media port range'
  udptl_last=$(( first + (last - first + 1) / 3 - 1 ))
  edit "$out_dir/udptl.conf" -e "s/^udptlstart=.*/udptlstart=$first/" -e "s/^udptlend=.*/udptlend=$udptl_last/"
  edit "$out_dir/rtp.conf" -e "s/^rtpstart=.*/rtpstart=$((udptl_last + 1))/" -e "s/^rtpend=.*/rtpend=$last/"
fi

# A phone system on the local network (Avaya IP Office or Aura) sends calls to
# the address docker-compose.phone-system.yml publishes Asterisk on: only that
# file sets FAXBOT_PHONE_SYSTEM_ADDRESS (from FAXBOT_LAN_ADDRESS in .env), with
# SIP on 5060 and at most 100 media ports. The record tells Faxbot what to give
# the phone system's administrator; without that file there is none.
lan_record=$shared/lan-address
if [ -n "${FAXBOT_PHONE_SYSTEM_ADDRESS:-}" ]; then
  [[ "$FAXBOT_PHONE_SYSTEM_ADDRESS" =~ ^$octet\.$octet\.$octet\.$octet$ ]] \
    || refuse 'Unsupported phone system address'
  [ -n "${FAXBOT_MEDIA_PORTS:-}" ] && (( last - first < 100 )) || refuse 'Unsupported media port range'
  temporary=$(mktemp "$shared/.lan-address.XXXXXX")
  printf '{"address": "%s", "sip_port": 5060, "media_ports": "%s-%s"}\n' \
    "$FAXBOT_PHONE_SYSTEM_ADDRESS" "$first" "$last" > "$temporary"
  mv -f "$temporary" "$lan_record"
else
  rm -f "$lan_record"
fi

# Only a Compose override that publishes the media range on this computer sets
# FAXBOT_MEDIA_PORTS (docker-compose.fax-ports.yml, .public.yml, .phone-system.yml).
# The record tells Faxbot its fax ports can be forwarded or opened on the router.
media_record=$shared/media-ports
if [ -n "${FAXBOT_MEDIA_PORTS:-}" ]; then
  temporary=$(mktemp "$shared/.media-ports.XXXXXX")
  printf '{"media_ports": "%s-%s"}\n' "$first" "$last" > "$temporary"
  mv -f "$temporary" "$media_record"
else
  rm -f "$media_record"
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
  # A phone system trunk names the address Faxbot is published on in the local
  # network, or Asterisk's own address when it is not published there.
  if [ -n "${FAXBOT_PHONE_SYSTEM_ADDRESS:-}" ]; then
    edit "$out_dir/pjsip.conf" -e "s/@FAXBOT_LAN_ADDRESS@/$FAXBOT_PHONE_SYSTEM_ADDRESS/g"
  else
    edit "$out_dir/pjsip.conf" -e '/@FAXBOT_LAN_ADDRESS@/d'
  fi
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

# The SSL Fax engine's fax lines. Faxbot writes iax.conf with the trunk files
# (Apply); without it chan_iax2 listens on loopback only and accepts no peer.
# Port 4569 is never published; the engine reaches it on the Compose network.
iax_conf=$shared/iax.conf
rm -f "$shared/iax.conf.started" "$engine_dir/iax.conf.started"
if [ -f "$iax_conf" ] && [ ! -L "$iax_conf" ]; then
  temporary=$(mktemp "$out_dir/.iax.conf.XXXXXX")
  cat "$iax_conf" > "$temporary"
  started=$(mktemp "$shared/.iax.conf.started.XXXXXX")
  cat "$temporary" > "$started"
  mv -f "$started" "$shared/iax.conf.started"
  # Only the engine's own line secrets are in this file.
  started=$(mktemp "$engine_dir/.iax.conf.started.XXXXXX")
  cat "$temporary" > "$started"
  chmod 644 "$started"
  mv -f "$started" "$engine_dir/iax.conf.started"
  mv -f "$temporary" "$out_dir/iax.conf"
else
  printf '%s\n' '[general]' 'bindaddr=127.0.0.1' 'bindport=4569' 'disallow=all' 'allow=ulaw' \
    'autokill=yes' 'delayreject=yes' > "$out_dir/iax.conf"
fi
# Faxbot's fax settings for received calls and the engine's lines (dialplan
# [faxbot-options]); the recommended values until Faxbot writes its own.
options_conf=$shared/extensions-options.conf
rm -f "$shared/extensions-options.conf.started"
if [ -f "$options_conf" ] && [ ! -L "$options_conf" ]; then
  temporary=$(mktemp "$out_dir/.extensions-options.conf.XXXXXX")
  cat "$options_conf" > "$temporary"
  started=$(mktemp "$shared/.extensions-options.conf.started.XXXXXX")
  cat "$temporary" > "$started"
  mv -f "$started" "$shared/extensions-options.conf.started"
  mv -f "$temporary" "$out_dir/extensions-options.conf"
else
  temporary=$(mktemp "$out_dir/.extensions-options.conf.XXXXXX")
  cat "$tpl_dir/extensions-options.conf" > "$temporary"
  mv -f "$temporary" "$out_dir/extensions-options.conf"
fi

# This Asterisk shares Faxbot's data folder, so Faxbot may restart it (over the
# manager connection, once no call is up) to load new settings; Docker's
# restart policy starts it again.
date +%s > "$shared/engine-started"
started=$(mktemp "$engine_dir/.asterisk-started.XXXXXX")
date +%s > "$started"
chmod 644 "$started"
mv -f "$started" "$engine_dir/asterisk-started"

# Without a login in the environment, follow the one Faxbot writes: when it
# appears or changes, stop gracefully (once no call is up) and Docker starts
# Asterisk again with it. First boot in Compose: both containers start; this
# Asterisk has no login yet and runs with its manager port off; when the SIP
# trunk first comes into use, Faxbot creates the login and writes it before it
# connects; this watcher restarts Asterisk within seconds and Faxbot's
# connection, which keeps retrying, logs in. An Asterisk started after Faxbot
# wrote the login reads it above.
if [ "$login_from" != environment ]; then
  login_sum() { if [ -f "$credentials" ]; then cksum < "$credentials"; else echo none; fi; }
  loaded=$(login_sum)
  (
    while sleep "${FAXBOT_LOGIN_CHECK_SECONDS:-5}"; do
      kill -0 "$$" 2>/dev/null || exit 0
      current=$(login_sum)
      if [ "$current" != "$loaded" ] \
          && "${FAXBOT_ASTERISK_CONTROL:-asterisk}" -rx 'core stop gracefully' >/dev/null 2>&1; then
        # In the container log: this restart came from the login, not from Faxbot's manager connection.
        # (Asterisk's own error output; this watcher keeps no pipe of the container open.)
        { printf 'faxbot-asterisk: the manager login changed; Asterisk restarts once no call is up\n' \
            > "/proc/$$/fd/2"; } 2>/dev/null || true
        exit 0
      fi
    done
  ) </dev/null >/dev/null 2>&1 &
fi

if [ -n "${FAXBOT_ASTERISK_COMMAND:-}" ]; then
  exec "$FAXBOT_ASTERISK_COMMAND"
fi
exec asterisk -f -C "$out_dir/asterisk.conf"
