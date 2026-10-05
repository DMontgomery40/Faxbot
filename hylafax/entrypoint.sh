#!/usr/bin/env bash
# Faxbot's fax engine: HylaFAX+ with IAXmodem lines registered to Faxbot's Asterisk.
#
# Faxbot writes the engine settings to <data>/hylafax/engine.conf (lines, their
# IAX secrets, the submission login, station identity, SSL Fax). Until that file
# exists the engine waits; when it changes, the engine restarts once no call is
# up and Docker starts it again with the new settings.
#
# One dial and one try per fax: faxq never redials or retries, and a job left in
# the send queue by a restart is moved aside, never sent again (Faxbot treats
# that fax as uncertain). Asterisk also refuses any call Faxbot did not ask for.
set -euo pipefail
umask 077

data=${FAXBOT_DATA:-/faxdata}
shared=$data/hylafax
conf=$shared/engine.conf
spool=${FAXBOT_HYLAFAX_SPOOL:-/var/spool/hylafax}
state=${FAXBOT_ENGINE_STATE:-/var/lib/faxbot-engine}
status=$shared/engine.status
check_seconds=${FAXBOT_ENGINE_CHECK_SECONDS:-5}
mkdir -p "$shared" "$state"

log() { printf 'faxbot-engine: %s\n' "$*" >&2; }
# refuse <sentence for the console> [detail for the container log]
refuse() { log "${2:-$1}"; write_status failed "$1"; exit 1; }
# Status sentences an office administrator reads on the trunk page (checked with Jev, 0.65-0.82).
NOT_STARTED="Faxbot's fast fax service could not start; select Apply and connect to try again."

write_status() {
  local temporary
  temporary=$(mktemp "$shared/.engine.status.XXXXXX")
  printf '{"state": "%s", "reason": "%s", "lines": %s, "listener": "%s", "at": %s, "version": "7.0.11"}\n' \
    "$1" "${2:-}" "${lines:-0}" "${listener:-}" "$(date +%s)" > "$temporary"
  chmod 644 "$temporary"
  mv -f "$temporary" "$status"
}

conf_sum() { if [ -f "$conf" ] && [ ! -L "$conf" ]; then cksum < "$conf"; else echo none; fi; }

if [ ! -f "$conf" ] || [ -L "$conf" ]; then
  write_status waiting "Faxbot's fast fax service starts when you select Apply and connect."
  log 'waiting for Faxbot to write the engine settings'
  while [ ! -f "$conf" ] || [ -L "$conf" ]; do sleep "$check_seconds"; done
fi
loaded=$(conf_sum)

# Read key=value lines; every value is checked before it reaches a config file.
declare -A setting=()
while IFS= read -r line || [ -n "$line" ]; do
  case $line in ''|'#'*) continue ;; esac
  key=${line%%=*}
  value=${line#*=}
  [[ "$key" =~ ^[a-z0-9_]{1,40}$ ]] || refuse "$NOT_STARTED" 'engine.conf has an unreadable line'
  setting[$key]=$value
done < "$conf"

get() { printf '%s' "${setting[$1]:-${2:-}}"; }
need() {
  local value
  value=$(get "$1")
  [[ "$value" =~ $2 ]] || refuse "$NOT_STARTED" "engine.conf setting $1 is missing or not usable"
  printf '%s' "$value"
}

lines=$(need lines '^[1-8]$')
asterisk_host=$(need asterisk_host '^[A-Za-z0-9.-]{1,253}$')
asterisk_port=$(need asterisk_port '^[0-9]{1,5}$')
submit_user=$(need submit_user '^[a-z][a-z0-9_]{0,31}$')
submit_password=$(need submit_password '^[A-Za-z0-9]{24,128}$')
station_id=$(need station_id '^[+0-9 ]{0,20}$')
fax_number=$(need fax_number '^[0-9]{0,20}$')
codec=$(need codec '^(ulaw|alaw)$')
sslfax=$(need sslfax '^(yes|no)$')
listener=$(need sslfax_listener '^([A-Za-z0-9.-]{1,253}:[0-9]{1,5})?$')
api_url=$(need api_url '^https?://[A-Za-z0-9.-]{1,253}(:[0-9]{1,5})?$')
secret=$(need inbound_secret '^[A-Za-z0-9_-]{16,256}$')
for line_number in $(seq 1 "$lines"); do
  need "line${line_number}_secret" '^[A-Za-z0-9]{24,128}$' >/dev/null
done

# One certificate and key for the SSL Fax listener, made once and kept in the
# engine's volume. HylaFAX+ reads the certificate first and then the key.
pem=$state/ssl.pem
if ! { [ -f "$pem" ] && head -1 "$pem" | grep -q 'BEGIN CERTIFICATE' && grep -q 'PRIVATE KEY' "$pem"; }; then
  workdir=$(mktemp -d)
  openssl req -x509 -newkey rsa:2048 -nodes -days 3650 -subj '/CN=Faxbot SSL Fax' \
    -keyout "$workdir/key.pem" -out "$workdir/cert.pem" >/dev/null 2>&1 \
    || refuse "Faxbot's fast fax service could not start; it will try again by itself." \
              'could not make the SSL Fax certificate'
  cat "$workdir/cert.pem" "$workdir/key.pem" > "$pem.new"
  mv -f "$pem.new" "$pem"
  rm -f "$workdir/key.pem" "$workdir/cert.pem"
  rmdir "$workdir"
fi
install -o uucp -g uucp -m 600 "$pem" "$spool/etc/ssl.pem"

# A fax job a restart left in the send queue is never sent again.
if compgen -G "$spool/sendq/q*" >/dev/null; then
  moved=$state/interrupted/$(date +%Y%m%d%H%M%S)
  mkdir -p "$moved"
  mv -f "$spool"/sendq/q* "$moved"/
  log "moved $(ls "$moved" | wc -l) unfinished job(s) aside; they are not sent again"
fi
rm -f "$spool/FIFO" "$spool"/FIFO.* 2>/dev/null || true

# Where results and received faxes go (read by the notify scripts as uucp).
printf 'url=%s\nsecret=%s\n' "$api_url" "$secret" > "$spool/etc/faxbot.conf"
chown uucp:uucp "$spool/etc/faxbot.conf"
chmod 600 "$spool/etc/faxbot.conf"

# Job submission login for Faxbot only; port 4559 stays on the private network.
# Inside the container, the engine's own status checks (faxstat) need no login.
hash=$(printf '%s' "$submit_password" | openssl passwd -6 -stdin)
printf '^%s@.*$:1001:%s\n^.*@127\\.0\\.0\\.1$\n' "$submit_user" "$hash" > "$spool/etc/hosts.hfaxd"
chown uucp:uucp "$spool/etc/hosts.hfaxd"
chmod 600 "$spool/etc/hosts.hfaxd"

# The scheduler: one dial and one try per job, one job per call, results to Faxbot.
cat > "$spool/etc/config" <<EOF
LogFacility:		daemon
ServerTracing:		0x00201
MaxDials:		1
MaxTries:		1
MaxBatchJobs:		1
NotifyCmd:		/usr/local/lib/faxbot-engine/notify
EOF
chown uucp:uucp "$spool/etc/config"

# Resolve Asterisk once here: IAXmodem wants an address.
asterisk_address=''
for attempt in $(seq 1 60); do
  asterisk_address=$(getent ahostsv4 "$asterisk_host" | awk 'NR == 1 {print $1}')
  [ -n "$asterisk_address" ] && break
  sleep 1
done
[ -n "$asterisk_address" ] || refuse "Faxbot's fast fax service cannot reach the phone connection." \
                                     "cannot resolve the Asterisk host $asterisk_host"

# The lines register only once Asterisk has loaded them: Apply writes them and
# restarts Asterisk, and a modem whose first registration is refused does not
# try again by itself. Asterisk's start script records what it loaded.
lines_loaded() {
  local started=$data/asterisk/iax.conf.started number
  [ -f "$started" ] || return 1
  for number in $(seq 1 "$lines"); do
    grep -qx "secret=$(get "line${number}_secret")" "$started" || return 1
  done
}
if [ -f "$data/asterisk/engine-started" ] && ! lines_loaded; then
  write_status waiting "Faxbot's fast fax service is waiting for the phone connection to restart."
  log 'waiting for Asterisk to load the fax lines'
  until lines_loaded; do sleep "$check_seconds"; done
  sleep "$check_seconds"
fi

mkdir -p /run/lock /etc/iaxmodem /var/log/iaxmodem
chmod 1777 /run/lock
[ -e /var/lock ] || ln -s /run/lock /var/lock
if [ "$sslfax" = yes ]; then ssl_support=Yes; else ssl_support=No; fi
# Session logs leave out HDLC frame dumps, modem byte traces and SSL Fax data:
# server, protocol, modem operations, timeouts and state changes only.
session_tracing=0x08117

for line_number in $(seq 1 "$lines"); do
  device=ttyIAX$line_number
  line_secret=$(get "line${line_number}_secret")
  cat > "/etc/iaxmodem/$device" <<EOF
device		/dev/$device
owner		uucp:uucp
mode		660
port		$((4569 + line_number))
refresh		60
server		$asterisk_address
peername	faxbot-line$line_number
secret		$line_secret
cidname		Faxbot
cidnumber	$fax_number
codec		$codec
EOF
  chmod 600 "/etc/iaxmodem/$device"
  {
    printf 'CountryCode:\t\t1\nAreaCode:\t\t\nLongDistancePrefix:\t1\nInternationalPrefix:\t011\n'
    printf 'FAXNumber:\t\t%s\n' "$fax_number"
    printf 'LocalIdentifier:\t"%s"\n' "$station_id"
    printf 'ServerTracing:\t\t0x00201\nSessionTracing:\t\t%s\n' "$session_tracing"
    printf 'RecvFileMode:\t\t0600\nLogFileMode:\t\t0600\nDeviceMode:\t\t0600\n'
    printf 'RingsBeforeAnswer:\t1\nSpeakerVolume:\t\toff\nGettyArgs:\t\t"-h %%l dx_%%s"\n'
    printf 'MaxRecvPages:\t\t200\nModemType:\t\tClass1\n'
    printf 'Class1AdaptRecvCmd:\tAT+FAR=1\nClass1TMConnectDelay:\t400\n'
    printf 'Class1RMQueryCmd:\t"!24,48,72,73,74,96,97,98,121,122,145,146"\n'
    printf 'Class1TMQueryCmd:\t"!24,48,72,73,74,96,97,98,121,122,145,146"\n'
    printf 'ModemResetCmds:\t\t"AT+VCID=1"\nModemReadyCmds:\t\tAT+FAR=1\n'
    printf 'Class1SSLFaxSupport:\t%s\nClass1SSLFaxCert:\tetc/ssl.pem\n' "$ssl_support"
    if [ "$sslfax" = yes ] && [ -n "$listener" ]; then
      printf 'Class1SSLFaxInfo:\t"%s"\n' "$listener"
    fi
  } > "$spool/etc/config.$device"
  chown uucp:uucp "$spool/etc/config.$device"
  iaxmodem -F "/etc/iaxmodem/$device" > "/var/log/iaxmodem/$device.log" 2>&1 &
done

for line_number in $(seq 1 "$lines"); do
  for attempt in $(seq 1 50); do
    [ -e "/dev/ttyIAX$line_number" ] && break
    sleep 0.2
  done
  [ -e "/dev/ttyIAX$line_number" ] || refuse "Fax line $line_number did not start."
done

faxq
hfaxd -i 4559
for line_number in $(seq 1 "$lines"); do
  faxgetty -D "ttyIAX$line_number"
done
write_status running ''
log "running with $lines fax line(s); SSL Fax $sslfax${listener:+, listener $listener}"

# Engine idle: no line is sending or receiving.
idle() {
  local busy
  busy=$(faxstat -s 2>/dev/null | grep -c -i -E 'sending|receiving|answering|dialing' || true)
  [ "$busy" = 0 ]
}

# A line whose registration Asterisk refused (Asterisk restarted underneath it)
# stays down; once it has been down for a while and no call is up, start again.
declare -A refused_since=()
registration_refused() {
  local number now
  now=$(date +%s)
  for number in $(seq 1 "$lines"); do
    if tail -n 1 "/var/log/iaxmodem/ttyIAX$number" 2>/dev/null | grep -q 'Registration failed'; then
      refused_since[$number]=${refused_since[$number]:-$now}
      (( now - refused_since[$number] >= 30 )) && return 0
    else
      unset "refused_since[$number]"
    fi
  done
  return 1
}

while sleep "$check_seconds"; do
  if registration_refused && idle; then
    write_status restarting "Faxbot's fast fax service is reconnecting to the phone connection."
    log 'a fax line was refused by Asterisk; restarting to register again'
    exit 1
  fi
  for daemon in faxq hfaxd iaxmodem faxgetty; do
    if ! pgrep -x "$daemon" >/dev/null; then
      write_status failed "Faxbot's fast fax service stopped and is starting again."
      log "$daemon stopped; restarting the engine"
      exit 1
    fi
  done
  current=$(conf_sum)
  if [ "$current" != "$loaded" ] && idle; then
    write_status restarting "Faxbot's fast fax service is loading new settings."
    log 'settings changed; restarting to load them'
    exit 0
  fi
done
