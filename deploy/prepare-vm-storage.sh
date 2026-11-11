#!/usr/bin/env bash
# Initialize storage and two distinct tokens. Existing tokens are preserved.
set -euo pipefail

if [[ "$(id -u)" != "0" ]]; then
    printf '%s\n' 'Run this initialization once with sudo.' >&2
    exit 1
fi
command -v openssl >/dev/null || {
    printf '%s\n' 'Install openssl before running this script.' >&2
    exit 1
}

deploy_dir="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
for directory in "$deploy_dir/secrets" "$deploy_dir/state" "$deploy_dir/state/data"; do
    if [[ -L "$directory" ]]; then
        printf 'Refusing a symlink directory: %s\n' "$directory" >&2
        exit 1
    fi
done
install -d -m 0700 "$deploy_dir/secrets"
install -d -m 0755 "$deploy_dir/state"
install -d -m 0700 -o 10001 -g 10001 "$deploy_dir/state/data"

for token_name in mcp_token worker_token; do
    token_path="$deploy_dir/secrets/${token_name}.txt"
    if [[ -L "$token_path" ]]; then
        printf 'Refusing a symlink token: %s\n' "$token_path" >&2
        exit 1
    fi
    if [[ ! -e "$token_path" ]]; then
        (umask 077; set -o noclobber; openssl rand -hex 32 > "$token_path")
    elif [[ ! -f "$token_path" || ! -s "$token_path" ]]; then
        printf 'Existing token must be a non-empty regular file: %s\n' "$token_path" >&2
        exit 1
    fi
    chown 0:10001 "$token_path"
    chmod 0440 "$token_path"
done
if cmp -s "$deploy_dir/secrets/mcp_token.txt" "$deploy_dir/secrets/worker_token.txt"; then
    printf '%s\n' 'MCP and worker tokens must be different. Existing files were preserved.' >&2
    exit 1
fi
printf '%s\n' 'Storage is ready. Two token files exist; token values were not printed.'
