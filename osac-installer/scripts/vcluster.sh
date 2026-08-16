#!/usr/bin/env bash
# vcluster lifecycle management for OSAC CI.
#
# Operations:
#   create   -- create vcluster, install lightweight cert-manager, 3-layer PKI, dual-CA ca-bundle
#   setup    -- copy secrets from host (Kafka SASL, AAP token, pull-secret), create Routes on host
#   teardown -- delete vcluster, clean up host Routes
#
# Required environment:
#   VCLUSTER_NAME  -- name of the vcluster (e.g. "pr-101")
#   VCLUSTER_NS    -- namespace on the host cluster to run the vcluster in
#
# Optional environment:
#   HOST_KUBECONFIG     -- kubeconfig for the host cluster (defaults to current context)
#   VCLUSTER_KUBECONFIG -- output path for the generated vcluster kubeconfig
#   VCLUSTER_VERSION    -- vcluster Helm chart version (default: 0.25.1)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/lib.sh"

VCLUSTER_NAME="${VCLUSTER_NAME:?"VCLUSTER_NAME must be set"}"
VCLUSTER_NS="${VCLUSTER_NS:?"VCLUSTER_NS must be set"}"
VCLUSTER_KUBECONFIG="${VCLUSTER_KUBECONFIG:-/tmp/vcluster-${VCLUSTER_NAME}.kubeconfig}"
VCLUSTER_VERSION="${VCLUSTER_VERSION:-0.25.1}"

# Cert-manager version for vcluster-internal PKI (lightweight, no OLM).
CERT_MANAGER_VERSION="${CERT_MANAGER_VERSION:-v1.17.2}"
TRUST_MANAGER_VERSION="${TRUST_MANAGER_VERSION:-v0.22.0}"

# Host-cluster oc wrapper: always targets the host, not the vcluster.
host_oc() {
    if [[ -n "${HOST_KUBECONFIG:-}" ]]; then
        command oc --kubeconfig "${HOST_KUBECONFIG}" "$@"
    else
        command oc "$@"
    fi
}

# vcluster-internal oc wrapper: targets the vcluster.
vcluster_oc() {
    command oc --kubeconfig "${VCLUSTER_KUBECONFIG}" "$@"
}

# ---------------------------------------------------------------------------
# create -- provision vcluster + internal PKI
# ---------------------------------------------------------------------------
cmd_create() {
    echo "=== Creating vcluster ${VCLUSTER_NAME} in namespace ${VCLUSTER_NS} ==="

    # Ensure the host namespace exists.
    host_oc create namespace "${VCLUSTER_NS}" --dry-run=client -o yaml | host_oc apply -f -

    # Deploy vcluster via Helm.
    helm upgrade --install "${VCLUSTER_NAME}" \
        oci://ghcr.io/loft-sh/charts/vcluster \
        --version "${VCLUSTER_VERSION}" \
        --namespace "${VCLUSTER_NS}" \
        --set controlPlane.distro.k8s.enabled=true \
        --set controlPlane.distro.k8s.apiServer.extraArgs='{--service-account-issuer=https://kubernetes.default.svc}' \
        --set sync.toHost.persistentVolumes.enabled=true \
        --set sync.toHost.storageClasses.enabled=true \
        --wait --timeout 10m

    echo "Waiting for vcluster to be ready..."
    retry_until 300 5 "host_oc get pod -n ${VCLUSTER_NS} -l app=${VCLUSTER_NAME} -o jsonpath='{.items[0].status.phase}' 2>/dev/null | grep -q Running"

    # Export kubeconfig for the vcluster.
    vcluster connect "${VCLUSTER_NAME}" \
        --namespace "${VCLUSTER_NS}" \
        --kube-config "${VCLUSTER_KUBECONFIG}" \
        --update-current=false

    echo "vcluster kubeconfig written to ${VCLUSTER_KUBECONFIG}"

    # Install lightweight cert-manager inside the vcluster (Helm, not OLM).
    echo "Installing cert-manager ${CERT_MANAGER_VERSION} inside vcluster..."
    helm upgrade --install cert-manager oci://quay.io/jetstack/charts/cert-manager \
        --version "${CERT_MANAGER_VERSION}" \
        --namespace cert-manager --create-namespace \
        --set crds.enabled=true \
        --kubeconfig "${VCLUSTER_KUBECONFIG}" \
        --wait --timeout 5m

    echo "Installing trust-manager ${TRUST_MANAGER_VERSION} inside vcluster..."
    helm upgrade --install trust-manager oci://quay.io/jetstack/charts/trust-manager \
        --version "${TRUST_MANAGER_VERSION}" \
        --namespace cert-manager \
        --set defaultPackage.enabled=false \
        --kubeconfig "${VCLUSTER_KUBECONFIG}" \
        --wait --timeout 5m

    # 3-layer PKI chain: self-signed root -> intermediate CA -> leaf issuer.
    echo "Creating 3-layer PKI chain..."
    vcluster_oc apply -f - <<'PKI_EOF'
apiVersion: cert-manager.io/v1
kind: Issuer
metadata:
  name: selfsigned-root
  namespace: cert-manager
spec:
  selfSigned: {}
---
apiVersion: cert-manager.io/v1
kind: Certificate
metadata:
  name: root-ca
  namespace: cert-manager
spec:
  isCA: true
  commonName: osac-vcluster-root-ca
  secretName: root-ca
  duration: 87600h
  privateKey:
    algorithm: RSA
    size: 4096
  issuerRef:
    name: selfsigned-root
    kind: Issuer
---
apiVersion: cert-manager.io/v1
kind: Issuer
metadata:
  name: root-ca-issuer
  namespace: cert-manager
spec:
  ca:
    secretName: root-ca
---
apiVersion: cert-manager.io/v1
kind: Certificate
metadata:
  name: intermediate-ca
  namespace: cert-manager
spec:
  isCA: true
  commonName: osac-vcluster-intermediate-ca
  secretName: intermediate-ca
  duration: 43800h
  privateKey:
    algorithm: RSA
    size: 4096
  issuerRef:
    name: root-ca-issuer
    kind: Issuer
---
apiVersion: cert-manager.io/v1
kind: ClusterIssuer
metadata:
  name: default-ca
spec:
  ca:
    secretName: intermediate-ca
PKI_EOF

    echo "Waiting for PKI certificates to be ready..."
    vcluster_oc wait --for=condition=Ready certificate/root-ca -n cert-manager --timeout=120s
    vcluster_oc wait --for=condition=Ready certificate/intermediate-ca -n cert-manager --timeout=120s

    # Dual-CA trust bundle: both root + intermediate CAs available to workloads.
    echo "Creating dual-CA ca-bundle..."
    vcluster_oc apply -f - <<'BUNDLE_EOF'
apiVersion: trust.cert-manager.io/v1alpha1
kind: Bundle
metadata:
  name: ca-bundle
spec:
  sources:
  - secret:
      name: "root-ca"
      key: "ca.crt"
  - secret:
      name: "intermediate-ca"
      key: "ca.crt"
  target:
    configMap:
      key: bundle.pem
BUNDLE_EOF

    echo "=== vcluster ${VCLUSTER_NAME} created with PKI ==="
}

# ---------------------------------------------------------------------------
# setup -- copy host secrets into vcluster, create host Routes
# ---------------------------------------------------------------------------
cmd_setup() {
    echo "=== Setting up vcluster ${VCLUSTER_NAME} ==="

    local osac_ns="${OSAC_NS:-osac}"

    # Create the target namespace inside the vcluster.
    vcluster_oc create namespace "${osac_ns}" --dry-run=client -o yaml | vcluster_oc apply -f -

    # Copy secrets from host cluster into vcluster.
    local secrets_to_copy=(
        "pull-secret"
    )

    # Kafka SASL credentials (created by Strimzi KafkaUser).
    local kafka_user_secret="${KAFKA_USER_SECRET:-osac-metering}"
    if host_oc get secret "${kafka_user_secret}" -n osac-kafka &>/dev/null; then
        echo "Copying Kafka SASL secret ${kafka_user_secret}..."
        host_oc get secret "${kafka_user_secret}" -n osac-kafka -o json \
            | jq 'del(.metadata.namespace, .metadata.resourceVersion, .metadata.uid, .metadata.creationTimestamp, .metadata.ownerReferences, .metadata.managedFields)' \
            | vcluster_oc apply -n "${osac_ns}" -f -
    fi

    # AAP API token (if present on host).
    if host_oc get secret osac-aap-api-token -n "${osac_ns}" &>/dev/null; then
        echo "Copying AAP API token secret..."
        host_oc get secret osac-aap-api-token -n "${osac_ns}" -o json \
            | jq 'del(.metadata.namespace, .metadata.resourceVersion, .metadata.uid, .metadata.creationTimestamp, .metadata.ownerReferences, .metadata.managedFields)' \
            | vcluster_oc apply -n "${osac_ns}" -f -
    fi

    # Pull secret (if present in openshift-config on host).
    if host_oc get secret pull-secret -n openshift-config &>/dev/null; then
        echo "Copying pull-secret..."
        host_oc get secret pull-secret -n openshift-config -o json \
            | jq '.metadata.name = "pull-secret" | del(.metadata.namespace, .metadata.resourceVersion, .metadata.uid, .metadata.creationTimestamp, .metadata.ownerReferences, .metadata.managedFields)' \
            | vcluster_oc apply -n "${osac_ns}" -f -
    fi

    # Create Routes on the host cluster pointing to the vcluster's services.
    # The vcluster exposes services as <svc>-x-<vcluster-ns>-x-<vcluster-name>
    # on the host.
    local domain
    domain=$(host_oc get ingresses.config/cluster -o jsonpath='{.spec.domain}')

    local external_route="fulfillment-api-${VCLUSTER_NS}"
    local internal_route="fulfillment-internal-api-${VCLUSTER_NS}"

    echo "Creating host Routes for vcluster services..."

    # External API Route (passthrough TLS).
    host_oc apply -f - <<ROUTE_EOF
apiVersion: route.openshift.io/v1
kind: Route
metadata:
  name: ${external_route}
  namespace: ${VCLUSTER_NS}
  labels:
    osac.openshift.io/vcluster: "${VCLUSTER_NAME}"
spec:
  host: ${external_route}.${domain}
  to:
    kind: Service
    name: fulfillment-api-x-${osac_ns}-x-${VCLUSTER_NAME}
  port:
    targetPort: https
  tls:
    termination: passthrough
ROUTE_EOF

    # Internal API Route (passthrough TLS).
    host_oc apply -f - <<ROUTE_EOF
apiVersion: route.openshift.io/v1
kind: Route
metadata:
  name: ${internal_route}
  namespace: ${VCLUSTER_NS}
  labels:
    osac.openshift.io/vcluster: "${VCLUSTER_NAME}"
spec:
  host: ${internal_route}.${domain}
  to:
    kind: Service
    name: fulfillment-internal-api-x-${osac_ns}-x-${VCLUSTER_NAME}
  port:
    targetPort: https
  tls:
    termination: passthrough
ROUTE_EOF

    echo "=== vcluster setup complete ==="
    echo "External API: https://${external_route}.${domain}"
    echo "Internal API: https://${internal_route}.${domain}"
}

# ---------------------------------------------------------------------------
# teardown -- delete vcluster and clean up host resources
# ---------------------------------------------------------------------------
cmd_teardown() {
    echo "=== Tearing down vcluster ${VCLUSTER_NAME} ==="

    # Delete Routes on host cluster created during setup.
    echo "Cleaning up host Routes..."
    host_oc delete route -n "${VCLUSTER_NS}" -l "osac.openshift.io/vcluster=${VCLUSTER_NAME}" --ignore-not-found

    # Delete the vcluster Helm release.
    echo "Deleting vcluster Helm release..."
    helm uninstall "${VCLUSTER_NAME}" \
        --namespace "${VCLUSTER_NS}" \
        --ignore-not-found --wait --timeout 5m

    # Clean up PVCs left by the vcluster.
    echo "Cleaning up PVCs..."
    host_oc delete pvc -n "${VCLUSTER_NS}" -l "app=${VCLUSTER_NAME}" --ignore-not-found

    # Delete the host namespace if empty (only contains our resources).
    local remaining
    remaining=$(host_oc get all -n "${VCLUSTER_NS}" --no-headers 2>/dev/null | wc -l)
    if [[ "${remaining}" -eq 0 ]]; then
        echo "Deleting empty namespace ${VCLUSTER_NS}..."
        host_oc delete namespace "${VCLUSTER_NS}" --ignore-not-found --wait=false
    else
        echo "Namespace ${VCLUSTER_NS} still has ${remaining} resources, skipping deletion"
    fi

    # Remove the kubeconfig file.
    rm -f "${VCLUSTER_KUBECONFIG}"

    echo "=== vcluster teardown complete ==="
}

# ---------------------------------------------------------------------------
# Main dispatch
# ---------------------------------------------------------------------------
case "${1:?"Usage: $0 {create|setup|teardown}"}" in
    create)   cmd_create ;;
    setup)    cmd_setup ;;
    teardown) cmd_teardown ;;
    *)
        echo "ERROR: unknown command '$1' -- expected create, setup, or teardown" >&2
        exit 1
        ;;
esac
