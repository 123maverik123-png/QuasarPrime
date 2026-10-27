#!/bin/sh
# wifi-pin-xiaomi.sh - keep the Yandex Station on Xiaomi_7787 and nowhere else.
#
# The station's WifiManager keeps re-adding every network known to the account
# and keeps re-setting a huge priority (2222) on TP-Link, so plain remove/disable
# does not stick. This daemon therefore:
#   1) gives every Xiaomi_7787 entry a very high priority (wins over 2222);
#   2) poisons the PSK of every other network entry, so even if the manager
#      prefers it, the station physically cannot complete the 4-way handshake;
#   3) if the station is associated to anything but Xiaomi_7787, forces it back
#      with select_network/reassociate.
# Entries are kept (not removed) on purpose: removing makes the manager re-add
# them in a tight loop.
#
# start: /etc/init.d/S99zwifipin start   (runs at boot)
# stop : killall wifi-pin-xiaomi.sh

IF=wlan0
KEEP="Xiaomi_7787"
GOOD_PRIO=3000
BAD_PRIO=0
BAD_PSK="blocked-by-wifi-pin-xiaomi"
SLEEP=3
LOG=/data/opt/var/wifi-pin.log
CLI="wpa_cli -i $IF"

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*" >> "$LOG"; }

mkdir -p /data/opt/var 2>/dev/null

# keep the log small
if [ -f "$LOG" ]; then
  sz=$(wc -c < "$LOG" 2>/dev/null || echo 0)
  if [ "$sz" -gt 300000 ]; then tail -n 800 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"; fi
fi

log "started (pid $$): only '$KEEP', priority $GOOD_PRIO, other PSKs poisoned"

n=0
while true; do
  n=$((n + 1))
  LIST=$($CLI list_networks 2>/dev/null)
  if [ -n "$LIST" ]; then
    CUR=$($CLI status 2>/dev/null | awk -F= '$1 == "ssid" {print substr($0, 6)}')

    # Xiaomi entries: top priority
    XID=""
    for id in $(echo "$LIST" | awk -F'\t' -v keep="$KEEP" 'NR>1 && $2 == keep {print $1}'); do
      [ -z "$XID" ] && XID=$id
      p=$($CLI get_network "$id" priority 2>/dev/null)
      if [ "$p" != "$GOOD_PRIO" ]; then
        $CLI set_network "$id" priority "$GOOD_PRIO" >/dev/null 2>&1 && log "id=$id '$KEEP' priority $p -> $GOOD_PRIO"
      fi
    done

    # every other network: poison PSK + lowest priority
    for id in $(echo "$LIST" | awk -F'\t' -v keep="$KEEP" 'NR>1 && $2 != keep {print $1}'); do
      ssid=$(echo "$LIST" | awk -F'\t' -v i="$id" '$1 == i {print $2}')
      p=$($CLI get_network "$id" priority 2>/dev/null)
      if [ "$p" != "$BAD_PRIO" ]; then
        $CLI set_network "$id" priority "$BAD_PRIO" >/dev/null 2>&1
      fi
      $CLI set_network "$id" psk "\"$BAD_PSK\"" >/dev/null 2>&1
      # set_network on a running network drops it; log only occasionally
      if [ $((n % 20)) -eq 1 ]; then log "id=$id '$ssid' blocked (psk poisoned, prio $BAD_PRIO)"; fi
    done

    # force the station back if it sits on anything else
    if [ -n "$CUR" ] && [ "$CUR" != "$KEEP" ]; then
      log "associated to '$CUR' -> forcing '$KEEP' (id=$XID)"
      [ -n "$XID" ] && $CLI select_network "$XID" >/dev/null 2>&1
      $CLI reassociate >/dev/null 2>&1
    fi

    if [ $((n % 200)) -eq 0 ]; then
      log "heartbeat: state=$( $CLI status 2>/dev/null | awk -F= '$1 == "wpa_state" {print $2}' ) ssid='$CUR' nets=$(echo "$LIST" | grep -c .)"
    fi
  fi
  sleep $SLEEP
done
