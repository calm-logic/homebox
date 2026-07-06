#!/bin/sh
# Bring up wg0 from the mounted config and keep it in sync as the admin app
# rewrites it (roster changes). busybox sh has no process substitution, so we
# strip to a temp file and syncconf from that.
set -u
CONF=/etc/wireguard/wg0.conf

up() {
  [ -f "$CONF" ] || return 0
  if ip link show wg0 >/dev/null 2>&1; then
    if wg-quick strip wg0 > /tmp/wg0.stripped 2>/dev/null; then
      wg syncconf wg0 /tmp/wg0.stripped 2>/dev/null || { wg-quick down wg0 2>/dev/null; wg-quick up wg0 2>/dev/null; }
    fi
  else
    wg-quick up wg0 2>/dev/null || true
  fi
}

trap 'wg-quick down wg0 2>/dev/null; exit 0' TERM INT
up
while true; do
  sleep 10
  up
done
