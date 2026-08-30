#!/usr/bin/env bash
set -euo pipefail

# Reconciliation loop: watch for changes to default-ca Secret and update Route destinationCACertificate
LAST_CA_CERT=""

reconcile() {
  # Check if Secret exists
  if ! oc get secret default-ca -n cert-manager &>/dev/null; then
    echo "default-ca secret not found, waiting..."
    return 1
  fi

  # Read current CA cert from Secret
  CA_CERT=$(oc get secret default-ca -n cert-manager -o jsonpath='{.data.ca\.crt}' | base64 -d)

  # Skip if cert hasn't changed
  if [[ "$CA_CERT" == "$LAST_CA_CERT" ]]; then
    return 0
  fi

  echo "CA certificate changed, patching keycloak Route..."

  # Check if Route exists
  if ! oc get route keycloak -n keycloak &>/dev/null; then
    echo "keycloak Route not found, waiting..."
    return 1
  fi

  # Read current Route TLS termination mode
  TERMINATION=$(oc get route keycloak -n keycloak -o jsonpath='{.spec.tls.termination}')

  # Only patch if termination is reencrypt (publicIngress mode)
  if [[ "$TERMINATION" != "reencrypt" ]]; then
    echo "Route termination is $TERMINATION (not reencrypt), skipping patch"
    return 0
  fi

  # Patch Route with new CA cert (use python3 for JSON escaping, available in all hook images)
  CA_CERT_JSON=$(python3 -c "import json,sys; print(json.dumps(sys.stdin.read()))" <<< "$CA_CERT")
  if oc patch route keycloak -n keycloak --type=json -p "[{
    \"op\": \"replace\",
    \"path\": \"/spec/tls/destinationCACertificate\",
    \"value\": $CA_CERT_JSON
  }]" 2>/dev/null; then
    echo "Successfully patched keycloak Route with updated destinationCACertificate"
    LAST_CA_CERT="$CA_CERT"
  else
    # If replace fails, try add (first time)
    if oc patch route keycloak -n keycloak --type=json -p "[{
      \"op\": \"add\",
      \"path\": \"/spec/tls/destinationCACertificate\",
      \"value\": $CA_CERT_JSON
    }]"; then
      echo "Added destinationCACertificate to keycloak Route"
      LAST_CA_CERT="$CA_CERT"
    else
      echo "ERROR: Failed to patch keycloak Route"
      return 1
    fi
  fi
}

# Initial reconciliation with retries
echo "Starting Route patch reconciliation..."
for i in $(seq 1 60); do
  if reconcile; then
    echo "Route patch reconciliation complete"
    exit 0
  fi
  [[ $i -eq 60 ]] && { echo "ERROR: Route patch reconciliation failed after 300s"; exit 1; }
  sleep 5
done
