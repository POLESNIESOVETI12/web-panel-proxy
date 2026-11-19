#!/usr/bin/env bash
# Public Telegram MTProxy bootstrap data, fetched via verified HTTPS on 2026-10-31.
# These are public protocol files, not an administrator's proxy access secret.

if [[ -z "${WEB_PANEL_PROXY_MTPROXY_DATA_DIR:-}" ]]; then
    WEB_PANEL_PROXY_MTPROXY_DATA_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/assets/mtproxy-bootstrap"
fi
export WEB_PANEL_PROXY_MTPROXY_DATA_DIR

wpp_mtproxy_validate_data() {
    local name="$1" file="$2" size
    [[ -f "$file" && ! -L "$file" ]] || return 1
    size="$(wc -c < "$file")"
    if [[ "$name" == proxy-secret ]]; then
        [[ "$size" -eq 128 ]]
    else
        [[ "$size" -ge 100 && "$size" -le 65536 ]] &&
            grep -q '^default ' "$file" && grep -q '^proxy_for ' "$file"
    fi
}

wpp_mtproxy_snapshot() {
    local name="$1" output="$2" digest file actual
    file="${WEB_PANEL_PROXY_MTPROXY_DATA_DIR}/$name"
    case "$name" in
        proxy-secret) digest=dc977517e8a064aae2d26d6a6d9dac6ddeff22e6e19edff87c8f6417d3454c0f ;;
        proxy-multi.conf) digest=196195d921f16e7815cabf9b843200d7edf1e8219ca2a99c38d393693f5c3c21 ;;
        *) return 2 ;;
    esac
    if ! wpp_mtproxy_validate_data "$name" "$file"; then
        echo "ERROR: Telegram offline file is missing or invalid: $name" >&2
        return 1
    fi
    actual="$(sha256sum "$file" | awk '{print $1}')"
    if [[ "$actual" != "$digest" ]]; then
        echo "ERROR: Telegram offline checksum mismatch: $name" >&2
        return 1
    fi
    cp -- "$file" "$output" || return
    echo "      Telegram $name: using verified public snapshot from 2026-10-31." >&2
}

curl() {
    local argument endpoint="" output="" expect_output=0 name code=0
    for argument in "$@"; do
        if (( expect_output )); then output="$argument"; expect_output=0; fi
        case "$argument" in
            --output|-o) expect_output=1 ;;
            --output=*) output="${argument#--output=}" ;;
            https://core.telegram.org/getProxySecret) endpoint="$argument"; name=proxy-secret ;;
            https://core.telegram.org/getProxyConfig) endpoint="$argument"; name=proxy-multi.conf ;;
        esac
    done
    # Other installer downloads keep their original behavior and verification.
    if [[ -z "$endpoint" ]]; then
        command curl "$@"
        return
    fi
    if [[ -z "$output" ]]; then
        echo "ERROR: MTProxy bootstrap download requires an output file." >&2
        return 2
    fi
    if [[ "${WEB_PANEL_PROXY_MTPROXY_OFFLINE:-0}" != 1 ]]; then
        if command curl -4 --connect-timeout 10 --max-time 30 --retry 1 --retry-delay 1 --retry-max-time 35 "$@"; then
            if wpp_mtproxy_validate_data "$name" "$output"; then return 0; fi
            echo "      Telegram $name: response validation failed." >&2
        else
            code=$?
            echo "      Telegram $name: download failed (curl $code). Trying the verified offline snapshot." >&2
        fi
    fi
    wpp_mtproxy_snapshot "$name" "$output"
}

export -f curl wpp_mtproxy_validate_data wpp_mtproxy_snapshot
