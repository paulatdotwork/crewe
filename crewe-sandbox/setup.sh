#!/usr/bin/env bash
# Idempotent setup for the Crewe sandbox egress infrastructure.
#   - crewe-egress      : --internal network (NO internet) — sandboxes live here
#   - crewe-egress-out  : normal bridge (HAS internet) — only the proxy is on it
#   - crewe-proxy       : tinyproxy, dual-homed on both, enforcing the allowlist
# Sandboxes get NO direct internet route; their only path out is the proxy.
set -euo pipefail
cd "$(dirname "$0")"

echo "== building images =="
docker build -q -t crewe-sandbox:latest -f Dockerfile.sandbox . >/dev/null && echo "  crewe-sandbox:latest ok"
docker build -q -t crewe-proxy:latest   -f Dockerfile.proxy   . >/dev/null && echo "  crewe-proxy:latest ok"

echo "== networks =="
docker network inspect crewe-egress     >/dev/null 2>&1 || docker network create --internal crewe-egress >/dev/null
docker network inspect crewe-egress-out >/dev/null 2>&1 || docker network create           crewe-egress-out >/dev/null
echo "  crewe-egress (internal) + crewe-egress-out (internet) ok"

echo "== proxy =="
docker rm -f crewe-proxy >/dev/null 2>&1 || true
# start on the internal net, then also attach the internet-connected net
docker run -d --name crewe-proxy --restart unless-stopped \
    --network crewe-egress \
    --memory 128m --cpus 0.5 \
    crewe-proxy:latest >/dev/null
docker network connect crewe-egress-out crewe-proxy
echo "  crewe-proxy up (dual-homed)"

echo "== host firewall: block sandbox -> docker gateway =="
# --internal blocks LAN/internet but NOT the host's own gateway IP: without
# this rule, sandboxed code can reach every 0.0.0.0-bound host service raw
# (the router, model servers), completely bypassing the allowlist proxy.
# Container-to-container traffic (the proxy) does not traverse INPUT, so the
# proxy stays reachable.
SUBNET=$(docker network inspect crewe-egress -f '{{range .IPAM.Config}}{{.Subnet}}{{end}}')
GATEWAY=$(docker network inspect crewe-egress -f '{{range .IPAM.Config}}{{.Gateway}}{{end}}')
if [ -n "$SUBNET" ] && [ -n "$GATEWAY" ]; then
  if sudo -n true 2>/dev/null || [ "$(id -u)" = 0 ]; then
    SUDO=""; [ "$(id -u)" = 0 ] || SUDO="sudo -n"
    $SUDO iptables -C INPUT -s "$SUBNET" -d "$GATEWAY" -j DROP 2>/dev/null \
      || $SUDO iptables -I INPUT -s "$SUBNET" -d "$GATEWAY" -j DROP
    echo "  sandbox->gateway ($SUBNET -> $GATEWAY) blocked"
    echo "  NOTE: iptables rules do not survive reboot — persist with your"
    echo "  distro's tool (iptables-persistent / nftables) if needed."
  else
    echo "  !! run this as root to close the gateway hole:"
    echo "     iptables -I INPUT -s $SUBNET -d $GATEWAY -j DROP"
  fi
fi

echo "== done =="
