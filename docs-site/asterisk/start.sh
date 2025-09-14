#!/usr/bin/env bash
set -euo pipefail

# Template files with envsubst for SIP trunk credentials
tpl_dir=/etc/asterisk/templates
out_dir=/etc/asterisk

mkdir -p "$out_dir"

if [ -f "$tpl_dir/pjsip.conf.template" ]; then
  envsubst < "$tpl_dir/pjsip.conf.template" > "$out_dir/pjsip.conf"
fi
if [ -f "$tpl_dir/manager.conf.template" ]; then
  envsubst < "$tpl_dir/manager.conf.template" > "$out_dir/manager.conf"
fi

exec asterisk -f -vvv

