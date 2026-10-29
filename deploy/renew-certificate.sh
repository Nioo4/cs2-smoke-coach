#!/bin/sh
set -eu

expected_lineage='/etc/letsencrypt/live/smokeline'
archive_dir='/etc/letsencrypt/archive/smokeline'

permissions_only=false
case "$#" in
    0) ;;
    1)
        if [ "$1" = '--permissions-only' ]; then
            permissions_only=true
        else
            echo 'unknown argument' >&2
            exit 2
        fi
        ;;
    *)
        echo 'unknown argument' >&2
        exit 2
        ;;
esac

if [ "${RENEWED_LINEAGE:-}" != "$expected_lineage" ]; then
    echo 'unexpected RENEWED_LINEAGE' >&2
    exit 1
fi

for directory in /etc/letsencrypt /etc/letsencrypt/live /etc/letsencrypt/archive \
    "$expected_lineage" "$archive_dir"; do
    if [ ! -d "$directory" ] || [ -L "$directory" ]; then
        echo 'certificate directory is invalid' >&2
        exit 1
    fi
    # The container only needs path traversal; it must not list the host's
    # certificate store. Fail closed if the host cannot apply these modes.
    chown root:10002 "$directory"
    chmod 0710 "$directory"
done

for name in fullchain.pem privkey.pem; do
    live_path="$expected_lineage/$name"
    if [ ! -L "$live_path" ]; then
        echo 'certificate lineage link is missing' >&2
        exit 1
    fi
    resolved_path="$(readlink -f -- "$live_path")"
    case "$resolved_path" in
        "$archive_dir"/*) ;;
        *)
            echo 'certificate lineage leaves the project archive' >&2
            exit 1
            ;;
    esac
    if [ ! -f "$resolved_path" ] || [ -L "$resolved_path" ] || [ ! -r "$resolved_path" ]; then
        echo 'renewed certificate target is invalid' >&2
        exit 1
    fi
    # Certbot owns the symlinks; only the resolved project files are adjusted.
    chown root:10002 "$resolved_path"
    chmod 0640 "$resolved_path"
done

if [ "$permissions_only" = true ]; then
    exit 0
fi

project_root=/opt/smokeline
compose() {
    docker compose --project-directory "$project_root" \
        -f "$project_root/compose.yml" -f "$project_root/compose.prod.yml" "$@"
}

compose exec -T nginx nginx -t -c /tmp/nginx.conf
compose exec -T nginx nginx -s reload -c /tmp/nginx.conf
