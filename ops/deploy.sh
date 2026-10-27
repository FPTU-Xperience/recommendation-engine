#!/usr/bin/env bash
set -Eeuo pipefail
umask 077
: "${DEPLOY_ROOT:?}" "${RELEASE_SHA:?}" "${RECOMMENDATION_TOKEN:?}" "${MONGODB_PASSWORD:?}"
[[ "$RELEASE_SHA" =~ ^[0-9a-f]{40}$ ]] || exit 1
[[ ${#RECOMMENDATION_TOKEN} -ge 32 && ${#RECOMMENDATION_TOKEN} -le 256 && "$RECOMMENDATION_TOKEN" != *$'\n'* ]] || { echo 'Use a 32-256 character recommendation token.' >&2; exit 1; }
[[ "$MONGODB_PASSWORD" =~ ^[A-Za-z0-9_-]{32,128}$ ]] || { echo 'Use a 32-128 character URL-safe Mongo password.' >&2; exit 1; }
export API_NETWORK="${API_NETWORK:-fptux-clubhub-api_internal-net}"
export RECOMMENDATION_IMAGE="fptux-recommendation:${RELEASE_SHA}"
docker network inspect "$API_NETWORK" >/dev/null
release="${DEPLOY_ROOT}/releases/${RELEASE_SHA}"
cd "$release"
previous="$(readlink -f "${DEPLOY_ROOT}/current" 2>/dev/null || true)"
if [[ -f "${DEPLOY_ROOT}/mongo-password.sha256" ]]; then
  expected="$(cat "${DEPLOY_ROOT}/mongo-password.sha256")"
  actual="$(printf '%s' "$MONGODB_PASSWORD" | sha256sum | cut -d ' ' -f 1)"
  [[ "$expected" == "$actual" ]] || { echo 'Mongo password changed: initialization variables do not rotate existing credentials. Rotate through Mongo first.' >&2; exit 1; }
fi
docker compose -p fptux-recommendation config --quiet
docker compose -p fptux-recommendation build --pull recommendation-service
# Never delete or recreate the database container/volume during an application rollout.
docker compose -p fptux-recommendation up -d --no-recreate recommendation-mongo
printf '%s' "$MONGODB_PASSWORD" | sha256sum | cut -d ' ' -f 1 > "${DEPLOY_ROOT}/mongo-password.sha256"
# Ensure rollback resolves the previous immutable image, rather than the new tag.
printf 'RECOMMENDATION_IMAGE=%s\nAPI_NETWORK=%s\n' "$RECOMMENDATION_IMAGE" "$API_NETWORK" > .env
rollback() {
  trap - ERR
  if [[ -n "$previous" && -f "$previous/docker-compose.yml" && -f "$previous/.env" ]]; then
    previous_image="$(sed -n 's/^RECOMMENDATION_IMAGE=//p' "$previous/.env")"
    (cd "$previous" && RECOMMENDATION_IMAGE="$previous_image" docker compose -p fptux-recommendation up -d --no-build --no-deps recommendation-service)
  fi
}
trap rollback ERR
docker compose -p fptux-recommendation up -d --no-build --wait --wait-timeout 150 recommendation-service
docker compose -p fptux-recommendation exec -T recommendation-service python -c \
  "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health/ready', timeout=5)"
ln -sfn "$release" "${DEPLOY_ROOT}/current"
trap - ERR
echo 'Recommendation release healthy. No public ports added; API stack was not restarted.'
