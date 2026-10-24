#!/bin/bash
# Fallback dynv6 dynamic DNS updater (in addition to the ddclient container).
# Intended to run from cron, e.g. every 30 minutes.

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
set -a; source "$ROOT/.env"; set +a

zone="${DYNV6_ZONE:?Set DYNV6_ZONE in .env}"
token="${DYNV6_TOKEN:?Set DYNV6_TOKEN in .env}"

# Current public IP
current_ip=$(curl -s ifconfig.me || curl -s icanhazip.com || curl -s ipinfo.io/ip)

# IP currently in DNS (queried against Google DNS to avoid local caching)
dns_ip=$(dig +short ${zone} @8.8.8.8)

# Only update if the IP has changed
if [ "$current_ip" != "$dns_ip" ]; then
    echo "$(date): IP changed! Current: $current_ip, DNS: $dns_ip"
    # The IPv4-specific endpoint is more reliable
    result=$(curl -s "https://ipv4.dynv6.com/api/update?zone=${zone}&token=${token}&ipv4=${current_ip}")
    echo "$(date): Update result: $result"
else
    echo "$(date): IP unchanged ($current_ip)"
fi
