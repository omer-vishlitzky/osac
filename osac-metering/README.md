# osac-metering

> [!WARNING]
> Be mindful of the content you commit to this repository. Do not commit any
> material containing Red Hat confidential content, including information about
> future product development plans.

Metering pipeline for OSAC — folds fulfillment's per-tenant event topics into
usage, sends per-minute differences to the billing provider, and can rebuild
everything from Kafka.

## How it works

Fulfillment publishes every billable change as an event on each tenant's
`osac.events.<tenant>` topic. Three loops run against one PostgreSQL database:

| Loop | Runs | What it does |
|---|---|---|
| Fold | Continuously | Copies each event's facts into `history`, idempotently |
| Round | Every minute, per tenant | Catches up, computes expected usage from history and rules, and writes the difference with the ledger to the outbox |
| Relay | Continuously | Sends outbox heartbeats to `osac.metering.heartbeat.<tenant>` and advances the heartbeat offset |

Billing rules are data: versioned, immutable ConfigMaps
(`osac-metering-rules-<NNNN>`) holding complete meter definitions and an
`effective_at` time. The chart ships the default meters as revision 0001.

Heartbeats are CloudEvents (`osac.resource.heartbeat.v1`) carrying a usage
interval and a quantity, positive or negative. The billing provider adds them
up; it never replaces one, and it drops heartbeat IDs it has already seen.
That is what makes a crash between a Kafka send and the outbox delete safe: the
same rows are resent with the same IDs.

Everything is rebuildable: history from the event topics, the ledger from the
heartbeat topic, revisions from the ConfigMaps. Losing or restoring the
database never bills usage twice and never loses any.

## Components

| Directory | Description |
|-----------|-------------|
| `schema/` | Shared heartbeat schema — usage and heartbeat data types, resource type constants |
| `metering-service/` | Fold, heartbeat rounds, relay, rule revisions, and usage evaluation |
| `adapters/` | Provider adapter framework (`Runner`, Kafka consumer, dedup, retry, DLQ routing) and concrete adapters (`echo-adapter`, `m360-adapter`) |
| `charts/osac-metering/` | Helm chart for the metering subsystem, including rule revision 0001 and the policy that protects it |

## Build and Test

```bash
# schema (shared types — no build step, test + lint only)
cd schema
make test     # Run unit tests (ginkgo)
make lint     # Run golangci-lint
cd ..

# metering-service
cd metering-service
make test     # Run unit and integration tests (ginkgo + go test)
make lint     # Run golangci-lint
cd ..

# adapters
cd adapters
make test     # Run unit tests (ginkgo, recursive)
make lint     # Run golangci-lint
```

Integration tests run against real PostgreSQL and Kafka in containers; they
need `podman` or `docker`. The M360 contract test additionally needs
`M360_TEST_API_URL`, `M360_TEST_API_KEY`, and `M360_TEST_TENANT`, and skips
itself without them. Deploying an M360 test tenant is outside this
repository's scope.

## Deployment

The metering subsystem is deployed via the osac-installer umbrella chart with
`metering.enabled: true`. Prerequisites: AMQ Streams operator and Kafka
cluster (installed by osac-installer phases 1 and 2 with `kafka.enabled:
true`), and Kubernetes 1.30 or later for the admission policy that protects
rule revisions.

Kafka ACLs: metering reads event topics and reads and writes heartbeat
topics; the adapters read heartbeat topics and write dead-letter topics;
nobody but tenant onboarding creates the topics.

## Replaying dead-lettered heartbeats

Rejected heartbeats land in the tenant's `osac.metering.dlq.<tenant>` topic.
A notification stays on while it has unhandled records. Once the cause is
fixed, replay them:

1. Stop the adapter.
2. Reset its position on the tenant's heartbeat topic to the earliest
   unhandled record's original offset, for example with
   `kafka-consumer-groups --reset-offsets --group osac-metering-m360-adapter`.
3. Mark the DLQ handled up to its end.
4. Restart the adapter. The billing provider drops the heartbeats it already
   has.
