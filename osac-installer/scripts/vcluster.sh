#!/usr/bin/env bash
set -euo pipefail

ACTION="${1:?Usage: vcluster.sh <create|delete> <name>}"
VCLUSTER_NAME="${2:?Usage: vcluster.sh <create|delete> <name>}"
VCLUSTER_NS="vc-${VCLUSTER_NAME}"
HOST_KUBECONFIG="${HOST_KUBECONFIG:-${KUBECONFIG}}"
VCLUSTER_KUBECONFIG="${VCLUSTER_KUBECONFIG:-/tmp/vcluster-${VCLUSTER_NAME}.kubeconfig}"
BASE_NAMESPACE="${BASE_NAMESPACE:-osac-base}"

case "${ACTION}" in
  create)
    echo "Creating vcluster ${VCLUSTER_NAME}..."
    vcluster create "${VCLUSTER_NAME}" \
      --namespace "${VCLUSTER_NS}" \
      --connect=false \
      --update-current=false

    vcluster connect "${VCLUSTER_NAME}" \
      --namespace "${VCLUSTER_NS}" \
      --print > "${VCLUSTER_KUBECONFIG}"

    echo "Waiting for vcluster API server..."
    for i in $(seq 1 30); do
      if kubectl --kubeconfig="${VCLUSTER_KUBECONFIG}" get --raw /version >/dev/null 2>&1; then
        echo "vcluster API server ready."
        break
      fi
      if [ "$i" -eq 30 ]; then
        echo "ERROR: vcluster API server not ready after 5 minutes." >&2
        exit 1
      fi
      sleep 10
    done

    setup_certmanager
    setup_cabundle
    copy_secrets
    create_routes
    create_remote_kubeconfig

    echo "vcluster ${VCLUSTER_NAME} ready."
    echo "  Kubeconfig: ${VCLUSTER_KUBECONFIG}"
    ;;

  delete)
    echo "Deleting vcluster ${VCLUSTER_NAME}..."
    DOMAIN=$(oc --kubeconfig="${HOST_KUBECONFIG}" get ingresses.config/cluster \
      -o jsonpath='{.spec.domain}' 2>/dev/null || true)

    oc --kubeconfig="${HOST_KUBECONFIG}" delete route \
      fulfillment-api fulfillment-internal-api \
      -n "${VCLUSTER_NS}" --ignore-not-found 2>/dev/null || true

    vcluster delete "${VCLUSTER_NAME}" --namespace "${VCLUSTER_NS}" 2>/dev/null || true

    rm -f "${VCLUSTER_KUBECONFIG}"
    echo "vcluster ${VCLUSTER_NAME} deleted."
    ;;

  *)
    echo "ERROR: Unknown action '${ACTION}'. Use 'create' or 'delete'." >&2
    exit 1
    ;;
esac

setup_certmanager() {
  echo "Installing cert-manager inside vcluster..."
  kubectl --kubeconfig="${VCLUSTER_KUBECONFIG}" apply -f \
    https://github.com/cert-manager/cert-manager/releases/latest/download/cert-manager.yaml

  kubectl --kubeconfig="${VCLUSTER_KUBECONFIG}" wait \
    --for=condition=Available deployment/cert-manager \
    -n cert-manager --timeout=120s
  kubectl --kubeconfig="${VCLUSTER_KUBECONFIG}" wait \
    --for=condition=Available deployment/cert-manager-webhook \
    -n cert-manager --timeout=120s

  kubectl --kubeconfig="${VCLUSTER_KUBECONFIG}" apply -f - <<'EOF'
apiVersion: cert-manager.io/v1
kind: Issuer
metadata:
  name: selfsigned-issuer
  namespace: cert-manager
spec:
  selfSigned: {}
---
apiVersion: cert-manager.io/v1
kind: Certificate
metadata:
  name: default-ca-cert
  namespace: cert-manager
spec:
  isCA: true
  secretName: default-ca
  commonName: osac-vcluster-ca
  issuerRef:
    name: selfsigned-issuer
    kind: Issuer
---
apiVersion: cert-manager.io/v1
kind: ClusterIssuer
metadata:
  name: default-ca
spec:
  ca:
    secretName: default-ca
EOF

  kubectl --kubeconfig="${VCLUSTER_KUBECONFIG}" wait \
    --for=condition=Ready certificate/default-ca-cert \
    -n cert-manager --timeout=60s

  echo "cert-manager and PKI chain ready."
}

setup_cabundle() {
  echo "Creating ca-bundle ConfigMap..."
  local osac_ns="osac"
  kubectl --kubeconfig="${VCLUSTER_KUBECONFIG}" create namespace "${osac_ns}" \
    --dry-run=client -o yaml | kubectl --kubeconfig="${VCLUSTER_KUBECONFIG}" apply -f -

  local vcluster_ca host_ca
  vcluster_ca=$(kubectl --kubeconfig="${VCLUSTER_KUBECONFIG}" get secret default-ca \
    -n cert-manager -o jsonpath='{.data.ca\.crt}' | base64 -d)
  host_ca=$(kubectl --kubeconfig="${HOST_KUBECONFIG}" get secret default-ca \
    -n cert-manager -o jsonpath='{.data.ca\.crt}' | base64 -d)

  kubectl --kubeconfig="${VCLUSTER_KUBECONFIG}" create configmap ca-bundle \
    --from-literal=bundle.pem="${vcluster_ca}${host_ca}" \
    -n "${osac_ns}" --dry-run=client -o yaml \
    | kubectl --kubeconfig="${VCLUSTER_KUBECONFIG}" apply -f -

  echo "ca-bundle ConfigMap created (vcluster CA + host CA)."
}

copy_secrets() {
  echo "Copying secrets from host base namespace into vcluster..."
  local osac_ns="osac"

  for secret in osac-aap-api-token fulfillment-controller-credentials; do
    echo "  Copying ${secret}..."
    kubectl --kubeconfig="${HOST_KUBECONFIG}" get secret "${secret}" \
      -n "${BASE_NAMESPACE}" -o json \
      | jq 'del(.metadata.resourceVersion,.metadata.uid,.metadata.creationTimestamp,.metadata.ownerReferences,.metadata.labels,.metadata.annotations) | .metadata.namespace = "'"${osac_ns}"'"' \
      | kubectl --kubeconfig="${VCLUSTER_KUBECONFIG}" apply -f -
  done

  echo "  Creating system:admin impersonation binding..."
  kubectl --kubeconfig="${VCLUSTER_KUBECONFIG}" apply -f - <<'EOF'
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: system-admin-impersonation
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: cluster-admin
subjects:
- apiGroup: rbac.authorization.k8s.io
  kind: User
  name: system:admin
EOF
}

create_routes() {
  echo "Creating Routes on host cluster..."
  local domain
  domain=$(oc --kubeconfig="${HOST_KUBECONFIG}" get ingresses.config/cluster \
    -o jsonpath='{.spec.domain}')

  [[ -n "${domain}" ]] || { echo "ERROR: Could not determine cluster domain." >&2; exit 1; }

  for svc in fulfillment-api fulfillment-internal-api; do
    local synced_name="${svc}-x-osac-x-${VCLUSTER_NAME}"
    local hostname="${svc}-${VCLUSTER_NAME}.${domain}"
    echo "  Route: ${hostname} → ${synced_name}"
    oc --kubeconfig="${HOST_KUBECONFIG}" -n "${VCLUSTER_NS}" \
      create route passthrough "${svc}" \
      --service="${synced_name}" \
      --hostname="${hostname}" \
      --dry-run=client -o yaml \
      | oc --kubeconfig="${HOST_KUBECONFIG}" apply -f -
  done
}

create_remote_kubeconfig() {
  echo "Creating remote-cluster kubeconfig Secret inside vcluster..."
  local osac_ns="osac"
  kubectl --kubeconfig="${VCLUSTER_KUBECONFIG}" create secret generic \
    remote-cluster-kubeconfig \
    --from-file=kubeconfig="${HOST_KUBECONFIG}" \
    -n "${osac_ns}" --dry-run=client -o yaml \
    | kubectl --kubeconfig="${VCLUSTER_KUBECONFIG}" apply -f -
}
