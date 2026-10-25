"""Volume capacity metering: a volume bills gibibyte-seconds while it exists.

The volume is the only meter whose quantity multiplies a size (the provisioned
GiB) by time, so this journey pins the multiplier: each heartbeat's quantity
must equal GiB x interval seconds exactly, not approximately.

The volume is created through the private API the way the OSAC CSI driver
creates one, because a node-local (LVMS) volume needs the node topology
segment and the public VolumeSpec carries no topology field: node-local
volumes are CSI-only by design.
"""

from __future__ import annotations

import contextlib
import itertools
import subprocess
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from tests.e2e.core.grpc_client import PRIVATE_API, PUBLIC_API, GRPCClient
from tests.e2e.core.helpers import unique_name
from tests.e2e.core.k8s_client import K8sClient
from tests.e2e.core.metering import HEARTBEAT_TYPE, MeteringCollector, net_usage_past_by_dimension
from tests.e2e.core.runner import poll_until, run

pytestmark = [pytest.mark.regression, pytest.mark.metering]

VOLUME_SIZE_GIB = 5
HEARTBEAT_TIMEOUT: int = 300

# The CR stamps the API server writes carry whole seconds, while heartbeat
# intervals carry microseconds: comparisons between the two allow one second,
# like the compute metering test's tolerance.
CR_STAMP_TOLERANCE = timedelta(seconds=1)

# The topology segment the LVMS provisioner requires: a node-local volume pins
# its node, and the cluster under test is single-node.
NODE_TOPOLOGY_SEGMENT = "osac.io/node"


@pytest.fixture
def metering_project(grpc: GRPCClient):
    """A project the volume's billed dimensions are attributed to."""
    name = unique_name("e2e-volume-metering-project")
    project_id: str = grpc.create_project(name=name)
    yield name
    with contextlib.suppress(Exception):
        grpc.delete_project(project_id=project_id)


def _parse_time(value: str) -> datetime:
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    return datetime.fromisoformat(value)


def _usage_interval(event: dict[str, Any]) -> tuple[datetime, datetime, Decimal]:
    usage = event["data"]["usage"]
    return _parse_time(usage["from"]), _parse_time(usage["to"]), Decimal(usage["quantity"])


def _interval_seconds(start: datetime, end: datetime) -> Decimal:
    delta = end - start
    micros = (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds
    return Decimal(micros) / Decimal(1_000_000)


def _net_intervals(events: list[dict[str, Any]]) -> list[tuple[datetime, datetime, Decimal]]:
    """Collapse signed heartbeats into the intervals the meter actually bills.

    Each heartbeat bills its quantity over its usage interval and a negative
    correction cancels the overlap, so a canceled tail disappears from the net
    instead of showing up as a gap or an overlap between raw intervals.
    Return the contiguous net-billed intervals with their net quantity.
    """
    rates = [
        (start, end, quantity / _interval_seconds(start, end))
        for start, end, quantity in (_usage_interval(event) for event in events)
    ]
    boundaries = sorted({moment for start, end, _ in rates for moment in (start, end)})
    intervals: list[tuple[datetime, datetime, Decimal]] = []
    for start, end in itertools.pairwise(boundaries):
        rate = sum(r for r_start, r_end, r in rates if r_start <= start and r_end >= end)
        if rate <= 0:
            continue
        quantity = rate * _interval_seconds(start, end)
        if intervals and intervals[-1][1] == start:
            previous_start, _, previous_quantity = intervals[-1]
            intervals[-1] = (previous_start, end, previous_quantity + quantity)
        else:
            intervals.append((start, end, quantity))
    return intervals


def _typed_heartbeats(metering: MeteringCollector, volume_id: str) -> list[dict[str, Any]]:
    """The heartbeats that carry the storage_tier dimension, in interval order."""
    typed = [
        event
        for event in metering.get_all_events(HEARTBEAT_TYPE, volume_id)
        if event.get("data", {}).get("billing_dimensions", {}).get("storage_tier")
    ]
    typed.sort(key=lambda event: event["data"]["usage"]["from"])
    return typed


def _final_heartbeat(metering: MeteringCollector, volume_id: str, deletion: datetime) -> dict[str, Any] | None:
    """The latest typed heartbeat once its interval reaches the recorded deletion."""
    latest_end: datetime | None = None
    latest: dict[str, Any] | None = None
    for event in _typed_heartbeats(metering, volume_id):
        _, to, _ = _usage_interval(event)
        if latest_end is None or to > latest_end:
            latest_end, latest = to, event
    if latest is None or latest_end is None or latest_end < deletion - CR_STAMP_TOLERANCE:
        return None
    return latest


def test_volume_bills_gibibyte_seconds(
    grpc: GRPCClient,
    private_grpc: GRPCClient,
    k8s_hub_client: K8sClient,
    metering: MeteringCollector,
    metering_project: str,
    jwt_tenant: str,
) -> None:
    """An AVAILABLE volume bills capacity x size per interval; deletion bills the
    final partial interval up to the recorded deletion, then ends billing."""
    node = run("kubectl", "get", "nodes", "-o", "jsonpath={.items[0].metadata.name}")
    name = unique_name("e2e-volume-metering")
    response: dict[str, Any] = private_grpc.call(
        service=f"{PRIVATE_API}.Volumes/Create",
        data={
            "object": {
                # The tenant the JWT authenticates as; the private Create carries
                # it explicitly, like the CSI driver does.
                "metadata": {"name": name, "tenant": jwt_tenant, "project": metering_project},
                "spec": {
                    "storageTier": "local",
                    "sizeGib": VOLUME_SIZE_GIB,
                    "accessMode": "VOLUME_ACCESS_MODE_READ_WRITE_ONCE",
                    "topology": {"segments": {NODE_TOPOLOGY_SEGMENT: node}},
                },
            }
        },
    )
    volume_id: str = response["object"]["id"]
    volume_cr_name: str | None = None

    try:
        volume = poll_until(
            fn=lambda: private_grpc.call(service=f"{PRIVATE_API}.Volumes/Get", data={"id": volume_id}),
            until=lambda v: v.get("object", {}).get("status", {}).get("state") == "VOLUME_STATE_AVAILABLE",
            retries=60,
            delay=5,
            description=f"{name} volume AVAILABLE",
        )
        # grpcurl renders proto int64 fields as strings.
        assert int(volume["object"]["spec"]["sizeGib"]) == VOLUME_SIZE_GIB

        # Resolve the Volume CR before any delete can remove it: the feedback
        # controller finds it by the volume-uuid label, and the deletion
        # assertions below need its recorded deletion timestamp.
        volume_cr_name = poll_until(
            fn=lambda: k8s_hub_client.get_by_label(
                resource="volume",
                label=f"osac.openshift.io/volume-uuid={volume_id}",
                jsonpath="{.items[0].metadata.name}",
            ),
            until=lambda name: name != "",
            retries=30,
            delay=2,
            description=f"Volume CR for {volume_id}",
        )

        # The first heartbeat that carries the storage_tier dimension is the first
        # one billed under the capacity meter.
        heartbeat = metering.heartbeat_with_dimensions(volume_id, {"storage_tier": "local"}, timeout=HEARTBEAT_TIMEOUT)
        metering.verify_event(heartbeat, resource_id=volume_id)

        data = heartbeat["data"]
        assert data["meter"] == "capacity", f"meter is {data['meter']!r}, want 'capacity'"
        assert data["usage"]["unit"] == "gibibyte_second", f"unit is {data['usage']['unit']!r}"
        assert data["billing_dimensions"]["storage_tier"] == "local"
        assert data["billing_dimensions"]["project"] == metering_project

        # The multiplier: wait for a second heartbeat, then every typed heartbeat's
        # quantity must equal size x the exact microsecond length of its interval,
        # and consecutive intervals must be contiguous with no billed gap.
        poll_until(
            fn=lambda: len(_typed_heartbeats(metering, volume_id)),
            until=lambda count: count >= 2,
            retries=HEARTBEAT_TIMEOUT // 10,
            delay=10,
            description=f"two typed heartbeats of volume {volume_id}",
        )
        typed = _typed_heartbeats(metering, volume_id)
        for event in typed:
            start, end, quantity = _usage_interval(event)
            expected = Decimal(VOLUME_SIZE_GIB) * _interval_seconds(start, end)
            assert quantity == expected, (
                f"quantity {quantity} is not {VOLUME_SIZE_GIB} GiB x "
                f"{_interval_seconds(start, end)}s: the size multiplier is wrong"
            )
        for previous, following in itertools.pairwise(typed):
            previous_end = _parse_time(previous["data"]["usage"]["to"])
            following_start = _parse_time(following["data"]["usage"]["from"])
            assert following_start == previous_end, (
                f"billed intervals are not contiguous: {previous_end} then {following_start}"
            )
    finally:
        deletion_recorded = _delete_volume(private_grpc, k8s_hub_client, volume_id=volume_id, cr_name=volume_cr_name)

    deleted_after = datetime.now(UTC)
    assert deletion_recorded is not None, "the volume's CR vanished before its deletion timestamp was read"

    # The final heartbeat covers the partial interval up to the recorded
    # deletion; billing runs through DELETING, so the final interval ends at
    # that timestamp or later (at the DELETED transition). It arrives only in
    # the round after the delete, so poll for it.
    poll_until(
        fn=lambda: _final_heartbeat(metering, volume_id, deletion_recorded),
        until=lambda event: event is not None,
        retries=HEARTBEAT_TIMEOUT // 10,
        delay=10,
        description=f"final partial-interval heartbeat of volume {volume_id}",
    )

    # The billed life is exact: the typed heartbeats collapse into contiguous
    # net intervals, each billing exactly size x its length in seconds, and the
    # last interval reaches the recorded deletion, so a dropped final partial
    # interval or a stale-phase overrun cannot pass.
    net = _net_intervals(_typed_heartbeats(metering, volume_id))
    assert net, f"no typed heartbeats of {volume_id} to assert the billed life on"
    for start, end, quantity in net:
        assert quantity == Decimal(VOLUME_SIZE_GIB) * _interval_seconds(start, end), (
            f"net interval {start}..{end} bills {quantity}, not {VOLUME_SIZE_GIB} GiB x its exact length in seconds"
        )
    for (_, previous_end, _), (start, _, _) in itertools.pairwise(net):
        assert start == previous_end, f"billed intervals are not contiguous: {previous_end} then {start}"
    assert net[-1][1] >= deletion_recorded - CR_STAMP_TOLERANCE, (
        f"the final interval ends at {net[-1][1]}, before the recorded deletion "
        f"at {deletion_recorded}: the partial final interval was dropped"
    )

    # Nothing bills past the removal. A volume legitimately bills past the
    # deletion request through DELETING until the physical volume is gone, so
    # the zero anchor is the post-removal moment, not the deletion timestamp.
    poll_until(
        fn=lambda: net_usage_past_by_dimension(metering.get_all_events(HEARTBEAT_TYPE, volume_id), past=deleted_after),
        until=lambda nets: all(abs(net) <= 1 for net in nets.values()),
        retries=HEARTBEAT_TIMEOUT // 10,
        delay=10,
        description=f"net billing of {volume_id} to end at the removal at {deleted_after}",
    )

    # Past the final interval nothing more arrives: the meter stopped.
    metering.assert_no_events(HEARTBEAT_TYPE, volume_id, since=deleted_after.isoformat(), within=120)


def _delete_volume(grpc: GRPCClient, k8s: K8sClient, *, volume_id: str, cr_name: str | None) -> datetime | None:
    """Delete the volume and return the deletion time the API server recorded.

    The CR lingers on its finalizers after the delete, so its deletion
    timestamp is readable before it disappears. None when the volume never had
    a CR to read: the cleanup path after a failed setup.
    """
    with contextlib.suppress(Exception):
        grpc.call(service=f"{PRIVATE_API}.Volumes/Delete", data={"id": volume_id})
    deletion_recorded: datetime | None = None
    if cr_name is not None:
        with contextlib.suppress(Exception):
            deletion_recorded = _parse_time(
                poll_until(
                    fn=lambda: k8s.get_jsonpath(
                        resource="volume", name=cr_name, jsonpath="{.metadata.deletionTimestamp}"
                    ),
                    until=lambda stamp: bool(stamp),
                    retries=30,
                    delay=2,
                    description=f"deletion timestamp of Volume CR {cr_name}",
                )
            )
        with contextlib.suppress(Exception):
            poll_until(
                fn=lambda: not k8s.is_present(resource="volume", name=cr_name),
                until=lambda gone: gone is True,
                retries=60,
                delay=5,
                description=f"{cr_name} Volume CR deletion",
            )
    poll_until(
        fn=lambda: _volume_gone(grpc, volume_id),
        until=lambda gone: gone is True,
        retries=60,
        delay=5,
        description=f"{volume_id} volume removed",
    )
    return deletion_recorded


def _volume_gone(grpc: GRPCClient, volume_id: str) -> bool:
    try:
        grpc.call(service=f"{PUBLIC_API}.Volumes/Get", data={"id": volume_id})
    except subprocess.CalledProcessError:
        return True
    return False
