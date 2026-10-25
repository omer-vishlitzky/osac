"""NAT gateway metering: a ready gateway bills allocation seconds.

The NAT gateway is the only billed resource whose dimensions are two
references (its virtual network and its external IP), and references render
by name in heartbeats, so this journey pins reference-based attribution.
"""

from __future__ import annotations

import contextlib
import itertools
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest

from tests.e2e.core.grpc_client import PUBLIC_API, GRPCClient
from tests.e2e.core.helpers import (
    wait_for_external_ip_allocated,
    wait_for_external_ip_cr,
    wait_for_external_ip_deletion,
    wait_for_nat_gateway_deletion,
)
from tests.e2e.core.k8s_client import K8sClient
from tests.e2e.core.metering import HEARTBEAT_TYPE, MeteringCollector, net_usage_past_by_dimension
from tests.e2e.core.runner import poll_until

pytestmark = pytest.mark.metering

HEARTBEAT_TIMEOUT: int = 300

# The CR stamps the API server writes carry whole seconds, while heartbeat
# intervals carry microseconds: comparisons between the two allow one second,
# like the compute metering test's tolerance.
CR_STAMP_TOLERANCE = timedelta(seconds=1)


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


def _final_heartbeat(metering: MeteringCollector, nat_id: str, deletion: datetime) -> dict[str, Any] | None:
    """The latest heartbeat once its interval reaches the recorded deletion."""
    latest_end: datetime | None = None
    latest: dict[str, Any] | None = None
    for event in metering.get_all_events(HEARTBEAT_TYPE, nat_id):
        _, to, _ = _usage_interval(event)
        if latest_end is None or to > latest_end:
            latest_end, latest = to, event
    if latest is None or latest_end is None or latest_end < deletion - CR_STAMP_TOLERANCE:
        return None
    return latest


@pytest.mark.requires_bmaas
def test_nat_gateway_bills_allocation(
    grpc: GRPCClient,
    k8s_hub_client: K8sClient,
    ref_virtual_network: dict[str, str],
    ref_eip_pool: dict[str, str],
    metering: MeteringCollector,
) -> None:
    """A READY gateway bills per interval with its references as dimensions;
    deletion bills the final partial interval up to the recorded deletion, then
    ends billing."""
    tag = uuid4().hex[:8]
    eip_name = f"ref-nat-metering-eip-{tag}"

    eip_response: dict[str, Any] = grpc.call(
        service=f"{PUBLIC_API}.ExternalIPs/Create",
        data={"object": {"metadata": {"name": eip_name}, "spec": {"pool": {"name": ref_eip_pool["name"]}}}},
    )
    eip_id: str = eip_response["object"]["id"]
    eip_cr_name = wait_for_external_ip_cr(k8s=k8s_hub_client, uuid=eip_id)
    nat_id: str | None = None
    nat_cr_name: str | None = None
    try:
        wait_for_external_ip_allocated(k8s=k8s_hub_client, name=eip_cr_name)

        nat_name = f"ref-nat-metering-{tag}"
        nat_id = grpc.create_nat_gateway(
            name=nat_name, virtual_network_name=ref_virtual_network["name"], external_ip_name=eip_name
        )
        poll_until(
            fn=lambda: (
                grpc.call(service=f"{PUBLIC_API}.NATGateways/Get", data={"id": nat_id})
                .get("object", {})
                .get("status", {})
                .get("state", "")
            ),
            until=lambda state: state in ("NAT_GATEWAY_STATE_READY", "Ready"),
            retries=60,
            delay=5,
            description=f"NATGateway {nat_name} READY",
        )
        # Resolve the CR before any delete can remove it: the deletion
        # assertions below need its recorded deletion timestamp.
        nat_cr_name = poll_until(
            fn=lambda: k8s_hub_client.get_nat_gateway_name(uuid=nat_id, checked=False),
            until=lambda name: name != "",
            retries=30,
            delay=5,
            description=f"NATGateway CR for {nat_name}",
        )

        heartbeat = metering.heartbeat_with_dimensions(
            nat_id, {"virtual_network": ref_virtual_network["name"]}, timeout=HEARTBEAT_TIMEOUT
        )
        # The gateway was created without a project, so its heartbeats must
        # carry no project dimension: the engine emits only the facts a
        # resource carries, and one that names a project is misattributed.
        metering.expect_no_project()
        metering.verify_event(heartbeat, resource_id=nat_id)

        data = heartbeat["data"]
        assert data["meter"] == "allocation", f"meter is {data['meter']!r}, want 'allocation'"
        assert data["usage"]["unit"] == "second", f"unit is {data['usage']['unit']!r}"
        assert data["billing_dimensions"]["virtual_network"] == ref_virtual_network["name"]
        assert data["billing_dimensions"]["external_ip"] == eip_name

        poll_until(
            fn=lambda: len(metering.get_all_events(HEARTBEAT_TYPE, nat_id)),
            until=lambda count: count >= 2,
            retries=HEARTBEAT_TIMEOUT // 10,
            delay=10,
            description=f"two heartbeats of NATGateway {nat_id}",
        )
        events = sorted(
            metering.get_all_events(HEARTBEAT_TYPE, nat_id), key=lambda event: event["data"]["usage"]["from"]
        )
        for event in events:
            start, end, quantity = _usage_interval(event)
            assert quantity == _interval_seconds(start, end), (
                f"quantity {quantity} is not the exact length of its interval ({start}..{end})"
            )
        for previous, following in itertools.pairwise(events):
            previous_end = _parse_time(previous["data"]["usage"]["to"])
            following_start = _parse_time(following["data"]["usage"]["from"])
            assert following_start == previous_end, (
                f"billed intervals are not contiguous: {previous_end} then {following_start}"
            )
    finally:
        deletion_recorded = (
            _delete_nat_gateway(grpc, k8s_hub_client, nat_id=nat_id, cr_name=nat_cr_name)
            if nat_id is not None
            else None
        )
        with contextlib.suppress(Exception):
            grpc.delete_external_ip(external_ip_id=eip_id)
            wait_for_external_ip_deletion(k8s=k8s_hub_client, name=eip_cr_name)

    deleted_after = datetime.now(UTC)
    if nat_id is not None:
        assert deletion_recorded is not None, "the gateway's CR vanished before its deletion timestamp was read"

        # The final heartbeat covers the partial interval up to the recorded
        # deletion, so its interval ends at that timestamp: it arrives only in
        # the round after the delete, so poll for it.
        poll_until(
            fn=lambda: _final_heartbeat(metering, nat_id, deletion_recorded),
            until=lambda event: event is not None,
            retries=HEARTBEAT_TIMEOUT // 10,
            delay=10,
            description=f"final partial-interval heartbeat of NATGateway {nat_id}",
        )

        # The billed life is exact: the heartbeats collapse into contiguous net
        # intervals, each billing exactly its length in seconds, and the last
        # interval reaches the recorded deletion, so a dropped final partial
        # interval or a stale-phase overrun cannot pass.
        net = _net_intervals(metering.get_all_events(HEARTBEAT_TYPE, nat_id))
        assert net, f"no heartbeats of {nat_id} to assert the billed life on"
        for start, end, quantity in net:
            assert quantity == _interval_seconds(start, end), (
                f"net interval {start}..{end} bills {quantity}, not its exact length in seconds"
            )
        for (_, previous_end, _), (start, _, _) in itertools.pairwise(net):
            assert start == previous_end, f"billed intervals are not contiguous: {previous_end} then {start}"
        assert net[-1][1] >= deletion_recorded - CR_STAMP_TOLERANCE, (
            f"the final interval ends at {net[-1][1]}, before the recorded deletion "
            f"at {deletion_recorded}: the partial final interval was dropped"
        )

        # Nothing bills past the removal.
        poll_until(
            fn=lambda: net_usage_past_by_dimension(metering.get_all_events(HEARTBEAT_TYPE, nat_id), past=deleted_after),
            until=lambda nets: all(abs(net) <= 1 for net in nets.values()),
            retries=HEARTBEAT_TIMEOUT // 10,
            delay=10,
            description=f"net billing of {nat_id} to end at the removal at {deleted_after}",
        )

        # Past the final interval nothing more arrives: the meter stopped.
        metering.assert_no_events(HEARTBEAT_TYPE, nat_id, since=deleted_after.isoformat(), within=120)


def _delete_nat_gateway(grpc: GRPCClient, k8s: K8sClient, *, nat_id: str, cr_name: str | None) -> datetime | None:
    """Delete the gateway and return the deletion time the API server recorded.

    The CR lingers on its finalizers after the delete, so its deletion
    timestamp is readable before it disappears. None when the gateway never
    had a CR to read: the cleanup path after a failed setup.
    """
    with contextlib.suppress(Exception):
        grpc.delete_nat_gateway(nat_gateway_id=nat_id)
    deletion_recorded: datetime | None = None
    if cr_name is not None:
        with contextlib.suppress(Exception):
            deletion_recorded = _parse_time(
                poll_until(
                    fn=lambda: k8s.get_jsonpath(
                        resource="natgateway", name=cr_name, jsonpath="{.metadata.deletionTimestamp}"
                    ),
                    until=lambda stamp: bool(stamp),
                    retries=30,
                    delay=2,
                    description=f"deletion timestamp of {cr_name}",
                )
            )
        with contextlib.suppress(Exception):
            wait_for_nat_gateway_deletion(k8s=k8s, name=cr_name)
    with contextlib.suppress(Exception):
        poll_until(
            fn=lambda: (
                nat_id
                not in [item["id"] for item in grpc.call(service=f"{PUBLIC_API}.NATGateways/List").get("items", [])]
            ),
            until=lambda gone: gone is True,
            retries=60,
            delay=5,
            description=f"NATGateway {nat_id} deletion",
        )
    return deletion_recorded
