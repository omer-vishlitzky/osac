#!/usr/bin/env bash
set -euo pipefail

# Pool-mode readiness polling. Replaces --wait for PROFILE=pool because
# the fulfillment-controller Deployment can't start until osac-create-creds
# Job creates the credential secret, and --wait would block before that
# Job completes (it's a regular Job, not a hook).
#
# Sequence:
#   1. Wait for Keycloak (unblocks osac-create-creds)
#   2. Wait for osac-create-creds Job to complete
#   3. Wait for all Deployments to be Available
#   4. Wait for post-install hook Jobs (they fire after helm returns)
#
# Usage: pool-wait-ready.sh <namespace>

NS="${1:?namespace is required}"
TIMEOUT="${POOL_WAIT_TIMEOUT:-600}"

die() { echo "ERROR: $*" >&2; exit 1; }
log() { echo "POOL-WAIT: $*"; }

wait_for() {
    local what="$1" resource="$2" for_expr="$3" timeout="${4:-${TIMEOUT}}"
    log "waiting for ${what}..."
    # Note: jsonpath waits use --for=jsonpath=... (NOT --for=condition=jsonpath=...).
    # Condition waits use --for=condition=Available.
    if ! oc wait --for="${for_expr}" "${resource}" -n "${NS}" --timeout="${timeout}s" 2>&1; then
        die "${what} not ready after ${timeout}s"
    fi
    log "${what} ready"
}

log "namespace: ${NS}, timeout: ${TIMEOUT}s"

# 1. Keycloak — unblocks osac-create-creds -> fulfillment-controller
wait_for "keycloak-database" statefulset/keycloak-database "jsonpath={.status.readyReplicas}=1" 300
wait_for "keycloak-service" deploy/keycloak-service "condition=Available" 300

# 2. osac-create-creds Job — creates fulfillment-controller-credentials
log "waiting for osac-create-creds Job..."
for i in $(seq 1 60); do
    status="$(oc get job osac-create-creds -n "${NS}" -o jsonpath='{.status.conditions[?(@.type=="Complete")].status}' 2>/dev/null)" || status=""
    if [[ "${status}" == "True" ]]; then
        log "osac-create-creds completed"
        break
    fi
    if [[ "${i}" -eq 60 ]]; then
        die "osac-create-creds Job not complete after 300s"
    fi
    sleep 5
done

# 2b. Restart fulfillment-controller so its OIDC discovery runs against a
#     live Keycloak. The controller's token source uses sync.Once for
#     discovery; if the initial attempt hit a not-yet-ready Keycloak the
#     token endpoint is cached as "" for the container's lifetime.
log "restarting fulfillment-controller for OIDC re-discovery..."
oc rollout restart deploy/fulfillment-controller -n "${NS}"

# 3. All Deployments
log "waiting for all Deployments..."
for deploy in $(oc get deploy -n "${NS}" -o name 2>/dev/null); do
    wait_for "${deploy}" "${deploy}" "condition=Available" "${TIMEOUT}"
done

# 4. Post-install hooks — they run after helm returns (no --wait).
# Poll for the heavyweight ones that the tests depend on.
log "waiting for post-install hook Jobs..."
HOOK_TIMEOUT=1800
elapsed=0
while true; do
    pending=0
    for job in $(oc get jobs -n "${NS}" -l "helm.sh/hook" -o name 2>/dev/null); do
        complete="$(oc get "${job}" -n "${NS}" -o jsonpath='{.status.conditions[?(@.type=="Complete")].status}' 2>/dev/null)" || complete=""
        failed="$(oc get "${job}" -n "${NS}" -o jsonpath='{.status.conditions[?(@.type=="Failed")].status}' 2>/dev/null)" || failed=""
        if [[ "${failed}" == "True" ]]; then
            die "hook ${job} failed"
        fi
        if [[ "${complete}" != "True" ]]; then
            pending=$((pending + 1))
        fi
    done
    if [[ "${pending}" -eq 0 ]]; then
        break
    fi
    if [[ "${elapsed}" -ge "${HOOK_TIMEOUT}" ]]; then
        die "${pending} hook Jobs still pending after ${HOOK_TIMEOUT}s"
    fi
    log "${pending} hook Jobs pending (${elapsed}s elapsed)..."
    sleep 15
    elapsed=$((elapsed + 15))
done

log "all Deployments ready, all hook Jobs complete"
