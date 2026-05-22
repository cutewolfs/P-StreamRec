#!/bin/sh
set -eu

log() {
    printf '%s\n' "p-streamrec-entrypoint: $*" >&2
}

is_truthy() {
    case "${1:-}" in
        1|true|TRUE|True|yes|YES|Yes|on|ON|On)
            return 0
            ;;
        *)
            return 1
            ;;
    esac
}

RESOLV_CONF="${PSTREAMREC_RESOLV_CONF:-/etc/resolv.conf}"
DNS_CACHE_LISTEN="${PSTREAMREC_DNS_CACHE_LISTEN:-127.0.0.1}"
DNS_CACHE_PORT="53"
ORIGINAL_RESOLV="${PSTREAMREC_DNS_CACHE_ORIGINAL_RESOLV:-/tmp/pstreamrec-resolv.conf}"
DNSMASQ_PID_FILE="${PSTREAMREC_DNS_CACHE_PID_FILE:-/tmp/pstreamrec-dnsmasq.pid}"
DNSMASQ_CMD="${PSTREAMREC_DNSMASQ_CMD:-dnsmasq}"

write_upstream_resolv_file() {
    : > "$ORIGINAL_RESOLV"

    if [ -n "${PSTREAMREC_DNS_CACHE_UPSTREAMS:-}" ]; then
        printf '%s\n' "$PSTREAMREC_DNS_CACHE_UPSTREAMS" \
            | tr ',;' '\n\n' \
            | while IFS= read -r server; do
                server="$(printf '%s' "$server" | tr -d '[:space:]')"
                if [ -n "$server" ]; then
                    printf 'nameserver %s\n' "$server"
                fi
            done > "$ORIGINAL_RESOLV"
        return
    fi

    if [ -r "$RESOLV_CONF" ]; then
        cat "$RESOLV_CONF" > "$ORIGINAL_RESOLV"
    fi
}

has_nameserver() {
    awk '$1 == "nameserver" { found = 1 } END { exit !found }' "$ORIGINAL_RESOLV"
}

has_unsafe_loopback_upstream() {
    awk '
        $1 == "nameserver" && ($2 == "127.0.0.1" || $2 == "::1") { found = 1 }
        END { exit !found }
    ' "$ORIGINAL_RESOLV"
}

write_cached_resolv_conf() {
    tmp_file="$(mktemp)"
    {
        printf 'nameserver %s\n' "$DNS_CACHE_LISTEN"
        awk '$1 == "search" || $1 == "domain" || $1 == "options" { print }' "$ORIGINAL_RESOLV"
    } > "$tmp_file"

    if ! cat "$tmp_file" > "$RESOLV_CONF"; then
        rm -f "$tmp_file"
        return 1
    fi

    rm -f "$tmp_file"
}

start_dns_cache() {
    if ! command -v "$DNSMASQ_CMD" >/dev/null 2>&1; then
        log "PSTREAMREC_DNS_CACHE=true but dnsmasq is not installed; continuing without local DNS cache"
        return 0
    fi

    write_upstream_resolv_file

    if ! has_nameserver; then
        log "PSTREAMREC_DNS_CACHE=true but no upstream nameserver was found; continuing without local DNS cache"
        return 0
    fi

    if [ -z "${PSTREAMREC_DNS_CACHE_UPSTREAMS:-}" ] && has_unsafe_loopback_upstream; then
        log "PSTREAMREC_DNS_CACHE=true but upstream DNS already points at localhost; set PSTREAMREC_DNS_CACHE_UPSTREAMS to avoid a resolver loop"
        return 0
    fi

    "$DNSMASQ_CMD" \
        --no-daemon \
        --no-hosts \
        --resolv-file="$ORIGINAL_RESOLV" \
        --listen-address="$DNS_CACHE_LISTEN" \
        --port="$DNS_CACHE_PORT" \
        --bind-interfaces \
        --cache-size="${PSTREAMREC_DNS_CACHE_SIZE:-10000}" \
        --pid-file="$DNSMASQ_PID_FILE" \
        &
    dnsmasq_pid="$!"

    sleep 0.2
    if ! kill -0 "$dnsmasq_pid" >/dev/null 2>&1; then
        log "dnsmasq failed to start; continuing without local DNS cache"
        return 0
    fi

    if ! write_cached_resolv_conf; then
        log "could not rewrite $RESOLV_CONF; stopping local DNS cache and continuing"
        kill "$dnsmasq_pid" >/dev/null 2>&1 || true
        return 0
    fi

    log "local DNS cache enabled on ${DNS_CACHE_LISTEN}:${DNS_CACHE_PORT}"
}

main() {
    if is_truthy "${PSTREAMREC_DNS_CACHE:-false}"; then
        start_dns_cache
    fi

    exec "$@"
}

if [ "${PSTREAMREC_ENTRYPOINT_TESTING:-}" = "1" ]; then
    return 0 2>/dev/null || exit 0
fi

main "$@"
