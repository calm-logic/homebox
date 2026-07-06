# Homebox cluster mesh — a tiny WireGuard node for the cross-node overlay.
# Runs with host networking + NET_ADMIN + /dev/net/tun; the admin app writes
# /etc/wireguard/wg0.conf (bind-mounted) and this keeps wg0 in sync with it.
FROM alpine:3.20
RUN apk add --no-cache wireguard-tools iproute2
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]
