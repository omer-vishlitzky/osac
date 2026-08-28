# Pool Cluster Architecture

This document describes the pool-cluster design for OSAC dev and E2E runs: a small
set of shared, long-lived OpenShift clusters where infrastructure is stamped once
and each run installs OSAC into its own namespace. It complements
[ARCHITECTURE.md](ARCHITECTURE.md), which covers product architecture. The
run-lifecycle contract consumed by osac-test-infra lives in
`osac-test-infra/docs/pool-env-contract.md`.

## Problem and Goal

A full OSAC install takes about an hour: OLM operator subscriptions take 15 to 20
minutes, the AAP bootstrap hook alone can take 10 to 40 minutes
(`osac-installer/docs/helm-deployment-guide.md:186`), and Phase 3 hooks (db-init,
config-as-code, Keycloak realm import) add more. E2E and dev runs pay this cost
every time, which caps how many runs CI can attempt per day and makes
test-failure triage slow.

The goal: a run reaches "tests start" in about 5 minutes, with negligible
marginal cost per run (roughly 2.5 to 3 GB of cluster memory per install), and N
concurrent runs per pool cluster, bounded only by admission caps (see
[Failure modes](#failure-modes)).

Constraint set, all hard:

- Same charts for CI, dev, and prod. No pool-only chart fork.
- Production changes are behavior-preserving refactors only: on default values,
  rendered manifests must be byte-identical to main.
- Tests may be mutated (namespace names, prefixes) but must keep testing the
  same things.
- Fail fast and loud everywhere. No fallbacks, no silent degradation, no
  best-effort retries around pool machinery.

## Topology

- K pool clusters. Each is a single-node OpenShift cluster; 256 GB RAM, 64 cores,
  2 TB NVMe recommended.
- 1 VM-pool machine: a libvirt host (qemu + ssh) that materializes the hardware
  OSAC sells. Agent VMs for CaaS run here (16 Gi each); BMH VMs for BMaaS run
  here too (8 Gi each), exposed to the cluster as bare-metal hosts. The
  VM-pool machine is keyed by run id: the cross-boundary reaper deletes libvirt
  domains and sushy processes by run id (see
  [Failure modes](#failure-modes)).
- Hardware materialized on the cluster (KubeVirt VMs, hosted clusters) or on the
  VM-pool machine (agent and BMH VMs). Both come from the same resource API;
  tests cannot tell the difference.

Memory budget per 256 GB cluster:

| Item | Cost | Lifetime |
|------|------|----------|
| Pool stamp (OLM operators, Kafka, PG, MCE, CNV, MetalLB) | ~30 to 35 GB | forever |
| Per-run OSAC install | ~2.5 to 3 GB RSS | one run |
| Transient per suite: vmaas | up to 4 VMs x 4 Gi | test duration |
| Transient per suite: caas | 5 to 8 GB hosted control plane | test duration |
| Transient per suite: bmaas | ~0 on cluster (BMH VMs live on the VM-pool machine) | test duration |

## Pool Stamping

Each pool cluster is stamped once, takes about 30 minutes, and is never
uninstalled. The stamp has three phases, driven by the existing installer:

1. **Phase 1, `osac-deps`** (full-ci profile): OLM operator subscriptions,
   including cert-manager, trust-manager, AAP, LVM, MetalLB, CNV, and MCE.
2. **Phase 2, `osac-infra`** (`osac-installer/charts/osac-infra/`): CA and
   ClusterIssuer; trust-manager Bundle with a label-based namespaceSelector so
   every per-run namespace trusts the pool CA; shared PostgreSQL sized with
   `max_connections=200`; Kafka broker plus 5 topics and the shared SASL user
   `osac-metering`; MetalLB pool widened to `.240` to `.254` to cover hosted
   cluster API load balancers; HyperConverged; MCE plus AgentServiceConfig;
   LVMCluster thinpool.
3. **Pool-system Phase 3 install**, one Helm release rendering only:
   the 13 operator CRDs (`operatorCrds.install=true`), the shared console proxy
   and its APIService (`v1alpha1.console.osac.openshift.io`,
   `osac-operator/charts/operator/templates/console-proxy/apiservice.yaml`),
   and the shared echo adapter with `ECHO_BUFFER_SIZE` raised
   (`osac-metering/adapters/cmd/echo-adapter/main.go:109` reads it). In
   addition, the bare-metal operator is configured with
   `watchAllNamespaces=true` so one BMO/Ironic serves all runs.

The stamped state is recorded as a fingerprint: a sha256 over the rendered CRDs,
the APIService, the CSIDriver, `values/*/infra.yaml` content, and the OLM
channels. The pool server refuses any run whose fingerprint does not match the
cluster's stamp (see [Failure modes](#failure-modes)).

## Per-Run Lifecycle

1. The run leases a pool cluster from the pool server. The server compares the
   run's fingerprint against the cluster stamp and refuses on mismatch, loudly.
2. The pool server hands back an env contract file (see
   `osac-test-infra/docs/pool-env-contract.md`) describing shared endpoints,
   credentials, and the per-run namespace.
3. The run installs OSAC into namespace `osac-<runid>` with
   `helm upgrade --install osac` and `values/pool/instance.yaml`:
   - `operatorCrds.install=false` and `bmfCrds.install=false`: CRDs are stamped
     (`osac-installer/charts/osac/values.yaml:18` for the default).
   - `csiDriver.enabled=false`: the CSIDriver object is cluster-scoped and
     stamped.
   - `operator.consoleProxy.disabled=true`: runs use the shared console proxy.
   - `rbacNameSuffix=<runid>`: per-run RBAC names cannot collide.
   - `aap.aap.instance.enabled=false` (default `true` at
     `osac-installer/charts/osac/values.yaml:109`): no per-run AAP. The
     bootstrap job runs remote against the shared AAP instance: `AAP_PREFIX`
     set to `<runid>`, organization `<runid>-org`, project created at the PR
     git URI and branch, subscription creation skipped, schedules gated off,
     and the EE image taken from the PR build.
   - Per-install Keycloak. The Keycloak block moves into the Phase 3 chart so a
     run gets its own Keycloak with its own realm named `osac`. The realm import
     stays gated by `realmOverwrite` as today
     (`osac-installer/charts/osac-infra/values.yaml:29`); because the Keycloak
     is per-install, the overwrite can only ever hit the run's own realm.
     (Why per-install: see [Failure modes](#failure-modes)).
   - Per-install OpenBao, in-memory.
   - Metering-service as producer only, pointing at the shared Kafka.
   - `dbInit.pgPrefix=<ns>` with the shared PG host
     (`osac-installer/charts/osac/values.yaml:7`,
     `osac-installer/charts/osac/templates/_helpers.tpl:38`): databases for the
     run are prefixed with the run namespace.
4. Post-install: config-as-code creates about 60 prefixed AAP objects, the
   prefixed job template is published, tests start.

Everything the run created is either namespaced under `osac-<runid>` or keyed
by the `<runid>` prefix, so the reaper can enumerate and delete it all without
touching other runs (see [Failure modes](#failure-modes)).

## Sharing Map

| Component | Shared or per-install | Why | Blast radius if it fails | How failure surfaces |
|-----------|----------------------|-----|--------------------------|----------------------|
| OLM operators | Shared (stamped) | Install cost dominates; nobody mutates them | Whole cluster | Subscriptions degrade; lease denied on re-hash mismatch |
| CNV (KubeVirt) | Shared (stamped) | Runs only create VMs | vmaas/caas suites on this cluster | VM creation fails; run fails loudly |
| MCE + assisted-service | Shared (stamped) | Hosted control planes are expensive to install | caas suite | HostedCluster creation fails; run fails loudly |
| BMO/Ironic | Shared, `watchAllNamespaces=true` | One Ironic serves all BMH VMs | bmaas suite | Power/provision actions fail; run fails loudly |
| Kafka broker + topics | Shared | One broker; topics are Go constants (`osac-metering/metering-service/internal/kafka/publisher.go:48`); `resource_id` filtering isolates runs (`osac-metering/schema/extensions.go:14`); metering-service is producer-only | Metering assertions across all runs | Producer publish errors fail the run; cross-talk shows up as assertion failures |
| Shared SASL user `osac-metering` | Shared | One credential, no per-run Kafka users | All runs' metering | Auth failure fails the run loudly |
| Shared echo adapter | Shared | Single consumer group; per-install adapters are disabled | Metering test assertions | Assertions fail in the run that owns the events; `resource_id` isolates ownership |
| PostgreSQL | Shared | One PG instance, per-run DB prefix, `pool_max_conns=4` per client, `max_connections=200` at stamping | All runs' DB access | pgx errors surface in fulfillment logs; run fails |
| Keycloak | Per-install | A shared realm would wipe tenant orgs on restart: `KC_SPI_IMPORT_REALM_FILE_STRATEGY=OVERWRITE` re-imports on boot, and the realm name is hardcoded `osac` in the fulfillment idp client (`fulfillment-service/internal/idp/client.go:115`) | Only the owning run | Pod failure fails the run |
| OpenBao | Per-install, in-memory | Cheap, no persistence needed in CI | Only the owning run | Pod failure fails the run |
| AAP | One shared instance | Install cost dominates; per-install config-as-code with `<runid>` prefix; job pods land in the run's namespace via `pod_spec_override` `metadata.namespace`; per-install `osac-sa` is cluster-admin | All runs' provisioning | Config-as-code fails at wait-for-aap; in-flight jobs queue |
| Console proxy + APIService | Shared (stamped) | Cluster-scoped APIService cannot be per-install; per-request namespace routing already exists (`osac-operator/internal/consoleproxy/subresource.go:25`) | Console tests in all runs | Console access fails; run fails |
| fulfillment, osac-operator, osac-ui, metering-service, bmf-operator | Per-install | Every run tests its own build | Only the owning run | Pod failure fails the run; operator watch predicates filter by `OSAC_*_NAMESPACE` env vars (`osac-operator/charts/operator/templates/deployment.yaml:73`) so N operators coexist |
| CRDs, APIService, CSIDriver, ClusterIssuer, Bundle | Cluster-scoped, stamped | Cannot be namespaced | Cluster-wide, fingerprint-gated | Lease refused on mismatch |
| MetalLB | Shared pool, `autoAssign` | Hosted-cluster API LBs take 1 IP each from `.240` to `.254`; test external-IP pools use `autoAssign: false` from per-slot CIDRs | Hosted cluster creation; external IP tests | IP exhaustion fails the run loudly |
| Tenant namespaces | Per-run names; namespace equals Tenant name | Isolation is the namespace | Only the owning run | Namespace-scoped failures fail the run |
| CUDNs | Cluster-scoped, name equals Subnet name | API shape; per-run unique names keep them collision-free | All runs on name collision | Collision is a create error; reaper removes leftovers |

## Failure Modes

Each entry: what happens, who notices, recovery.

### Fingerprint mismatch

The pool server refuses the lease and prints both hashes (cluster stamp and run
request). PRs touching cluster-scoped files are routed to a dedicated fresh
cluster running today's full-install path; the paths are `osac-operator/api/**`,
`operator-crds/**`, `console-proxy/apiservice.yaml`, `osac-csi-driver` charts,
`osac-deps/**`, `osac-infra/**`, `values/*/infra.yaml`. Both mechanisms exist
because a helm second install with a different release namespace fails on
ownership metadata (verified against helm 3.17 `pkg/action/validate.go`), and
force-applying divergent CRDs prunes fields on write with no error. The latter
is the nightmare case: silent field loss on a shared cluster. The fingerprint
makes it structurally impossible to install a run whose cluster-scoped state
does not match the stamp.

### Shared PostgreSQL exhausted

`pool_max_conns=4` per client plus `max_connections=200` at stamping bound
usage. If connections are still exhausted, new DB connections fail loudly:
pgx errors surface in fulfillment-service pod logs and the run fails. Recovery:
the pool server caps concurrent runs (see admission caps below), and a run that
leaks connections fails visibly in its own logs.

### Keycloak

Never shared. Each install gets its own Keycloak, and a restart re-imports only
its own realm. A shared realm plus
`KC_SPI_IMPORT_REALM_FILE_STRATEGY=OVERWRITE` would delete tenant orgs created
by other runs on every pod restart; the hardcoded realm name in
`fulfillment-service/internal/idp/client.go:115` means no per-run realm rename
is possible without a production change.

### AAP failure

If the shared AAP is down, the config-as-code bootstrap fails at the
wait-for-aap init container: the helm hook fails and the install fails, loudly.
In-flight AAP jobs queue on controller capacity rather than erroring. The pool
server enforces a global semaphore of 24 concurrent AAP jobs across clusters so
EE pod spikes cannot evict run pods.

### BestEffort eviction

Without resource requests, the kubelet evicts fulfillment and Keycloak pods
first and runs die randomly. Eliminated by adding resource requests to
fulfillment, Keycloak, and EE pods, and raising the operator memory limit. Who
notices: whoever's run dies randomly is the signal; after the fix, omission of
requests fails the render (charts always set them).

### Admission caps

vmaas 12, caas 10, bmaas 8 concurrent runs per 256 GB cluster, enforced by the
pool server at lease time. Exceeding a cap queues the run, it does not fail it.

### Reaper

Per-run teardown, in order: cancel running AAP jobs by `<runid>` prefix, wait
for HostedCluster teardown, strip VMI/BMH finalizers, delete the run
namespaces, then delete cluster-scoped leftovers by label: CUDNs labeled
`osac.openshift.io/managed-by`, per-tenant StorageClasses, and MetalLB parking
Services in `metallb-system`. Every leak the reaper cannot remove is reported
as a job artifact. Nothing is silently ignored. A scheduled cross-boundary
reaper covers resources that survive cluster reboots, keyed by run id: AAP
objects, libvirt domains on the VM-pool machine, sushy processes, and Route53
records.

### Pool host safety

`teardown.sh` and `make uninstall-infra`
(`osac-installer/Makefile:160`) must never run on a pool cluster: they delete
stamped state. The stamping release is uninstall-protected (helm
`helm.sh/resource-policy: keep` is already on the CRDs), and the pool server
holds the only helm lock for the stamping release, so nothing else can run a
mutating helm command against it.

### CRD or APIService drift mid-lease

Before extending any lease, the pool server re-hashes the live CRDs against the
recorded fingerprint. On mismatch it refuses the extension, loudly. Recovery:
drift is investigated manually; the cluster is re-stamped if needed.

### Pool cluster loss

Re-stamp from the script, about 30 minutes. A second pool cluster covers
capacity while one is down. The cross-boundary reaper still reaps orphans by
run id on the surviving infrastructure (AAP, libvirt, Route53).

## Spikes

Five open spikes gate specific parts of this design. Everything else in this
document is pinned to file:line evidence in the repos.

| Spike | What it gates |
|-------|---------------|
| AWX `pod_spec_override` `metadata.namespace` on AAP 2.5, plus controller service account name discovery | The "AAP jobs run in the run's namespace" mechanism; without it, shared AAP cannot host N runs safely |
| osac-ui render | Whether the UI chart can install per-run (RBAC suffix, namespace pinning) or needs a pool-mode value |
| EE pod security | Whether EE job pods can run with the required securityContext against the PR-built image |
| PostgreSQL load | Validates `max_connections=200` and `pool_max_conns=4` under 12 concurrent runs |
| End-to-end vmaas prototype | Proves the whole lifecycle: lease, install in ~5 min, tests, reaper, no cross-run bleed |

## Behavior-Preservation Gate

Every chart refactor must render byte-identical manifests on default values
against main. The script `scripts/pool/gates/helm-diff.sh` in osac-test-infra
enforces this in CI: it renders the charts on both refs and fails on any diff.
Production refactors land only as env- or value-plumbed behavior-preserving
changes: defaults render today's output exactly, pool behavior is opt-in via
values.
