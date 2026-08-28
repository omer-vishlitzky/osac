#!/usr/bin/env bash
set -euo pipefail

KEYCLOAK_NAMESPACE="${KEYCLOAK_NAMESPACE:?KEYCLOAK_NAMESPACE is required}"

echo "Waiting for keycloak-service deployment..."
oc wait --for=condition=Available deploy/keycloak-service -n "${KEYCLOAK_NAMESPACE}" --timeout=600s

echo "Keycloak is ready."
