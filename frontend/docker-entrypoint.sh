
#!/bin/sh
set -eu

: "${BASIC_AUTH_USER:?Set BASIC_AUTH_USER for the deployment}"
: "${BASIC_AUTH_PASSWORD:?Set BASIC_AUTH_PASSWORD for the deployment}"
: "${BACKEND_URL:?Set BACKEND_URL for the deployment}"

printf '%s\n' "$BASIC_AUTH_PASSWORD" | htpasswd -iB -c /etc/nginx/.htpasswd "$BASIC_AUTH_USER"

envsubst '${BACKEND_URL}' \
  < /etc/nginx/templates/default.conf.template \
  > /etc/nginx/conf.d/default.conf

exec nginx -g 'daemon off;'
