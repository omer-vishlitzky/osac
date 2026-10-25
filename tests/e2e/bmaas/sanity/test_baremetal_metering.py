"""Bare-metal instance metering: allocation and consumption bill side by side.

The bare-metal instance is the only resource with two meters on one resource:
``allocation`` bills the host from the moment provisioning confirms it (it
keeps billing through STOPPED, because the host stays reserved), while
``consumption`` bills only the spans the instance actually ran. Both carry the
instance_type dimension, so attribution holds on each meter separately.
"""

from __future__ import annotations

import contextlib
import itertools
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from tests.e2e.core.grpc_client import GRPCClient
from tests.e2e.core.helpers import (
    wait_for_bmi_cr,
    wait_for_bmi_deletion,
    wait_for_bmi_grpc_removal,
    wait_for_bmi_running,
)
from tests.e2e.core.k8s_client import K8sClient
from tests.e2e.core.metering import HEARTBEAT_TYPE, MeteringCollector, net_usage_past_by_dimension
from tests.e2e.core.osac_cli import OsacCLI
from tests.e2e.core.runner import poll_until

pytestmark = [pytest.mark.sanity, pytest.mark.metering]

HEARTBEAT_TIMEOUT: int = 600

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


def _final_heartbeat(
    metering: MeteringCollector, bmi_id: str, deletion: datetime, *, meter: str
) -> dict[str, Any] | None:
    """The latest typed heartbeat of one meter once its interval reaches the deletion."""
    latest_end: datetime | None = None
    latest: dict[str, Any] | None = None
    for event in _typed_heartbeats(metering, bmi_id, meter):
        _, to, _ = _usage_interval(event)
        if latest_end is None or to > latest_end:
            latest_end, latest = to, event
    if latest is None or latest_end is None or latest_end < deletion - CR_STAMP_TOLERANCE:
        return None
    return latest


def _typed_heartbeats(metering: MeteringCollector, bmi_id: str, meter: str) -> list[dict[str, Any]]:
    """The heartbeats of one meter that carry the instance_type dimension."""
    typed = [
        event
        for event in metering.get_all_events(HEARTBEAT_TYPE, bmi_id)
        if event.get("data", {}).get("meter") == meter
        and event.get("data", {}).get("billing_dimensions", {}).get("instance_type")
    ]
    typed.sort(key=lambda event: event["data"]["usage"]["from"])
    return typed


def _wait_for_meter(metering: MeteringCollector, bmi_id: str, meter: str, instance_type: str) -> dict[str, Any]:
    """Return the first heartbeat of the meter carrying the instance type."""
    return poll_until(
        fn=lambda: _typed_heartbeats(metering, bmi_id, meter),
        until=lambda events: len(events) > 0,
        retries=HEARTBEAT_TIMEOUT // 10,
        delay=10,
        description=f"first {meter} heartbeat of {bmi_id} with instance_type",
    )[0]


def _assert_contiguous_exact_seconds(events: list[dict[str, Any]], meter: str) -> None:
    """Every heartbeat bills exactly its interval; consecutive intervals touch."""
    assert events, f"no typed {meter} heartbeats to assert arithmetic on"
    for event in events:
        start, end, quantity = _usage_interval(event)
        assert quantity == _interval_seconds(start, end), (
            f"{meter} quantity {quantity} is not the exact length of its interval ({start}..{end})"
        )
    for previous, following in itertools.pairwise(events):
        previous_end = _parse_time(previous["data"]["usage"]["to"])
        following_start = _parse_time(following["data"]["usage"]["from"])
        assert following_start == previous_end, (
            f"{meter} billed intervals are not contiguous: {previous_end} then {following_start}"
        )


def test_baremetal_instance_bills_allocation_and_consumption(
    jwt_cli_user: OsacCLI,
    jwt_grpc_tenant1: GRPCClient,
    k8s_hub_client: K8sClient,
    metering: MeteringCollector,
    catalog_item: str,
    bmi_instance_type: str,
    bmi_disk_image: str,
    ssh_public_key: str,
    test_run_id: str,
) -> None:
    """Both meters bill a running instance and stop after its deletion, each
    ending at the recorded deletion with its final partial interval billed."""
    name = f"e2e-bmi-metering-{test_run_id}"
    bmi_id, _ = jwt_cli_user.create_baremetal_instance(
        name=name, catalog_item=catalog_item, ssh_key=ssh_public_key, disk_image=bmi_disk_image
    )
    bmi_cr_name: str | None = None

    try:
        bmi_cr_name = wait_for_bmi_cr(k8s=k8s_hub_client, uuid=bmi_id)
        wait_for_bmi_running(grpc=jwt_grpc_tenant1, bmi_id=bmi_id)

        allocation = _wait_for_meter(metering, bmi_id, "allocation", bmi_instance_type)
        # The instance is created without a project, so its heartbeats must
        # carry no project dimension: the engine emits only the facts a
        # resource carries, and one that names a project is misattributed.
        metering.expect_no_project()
        metering.verify_event(allocation, resource_id=bmi_id)
        consumption = _wait_for_meter(metering, bmi_id, "consumption", bmi_instance_type)
        metering.verify_event(consumption, resource_id=bmi_id)

        for heartbeat, meter in ((allocation, "allocation"), (consumption, "consumption")):
            data = heartbeat["data"]
            assert data["meter"] == meter
            assert data["usage"]["unit"] == "second", f"{meter} unit is {data['usage']['unit']!r}"
            assert data["billing_dimensions"]["instance_type"] == bmi_instance_type, (
                f"{meter} instance_type is {data['billing_dimensions'].get('instance_type')!r}"
            )

        # Wait for a second heartbeat of each meter, then pin the arithmetic.
        for meter in ("allocation", "consumption"):
            poll_until(
                fn=lambda meter=meter: len(_typed_heartbeats(metering, bmi_id, meter)),
                until=lambda count: count >= 2,
                retries=HEARTBEAT_TIMEOUT // 10,
                delay=10,
                description=f"two typed {meter} heartbeats of {bmi_id}",
            )
            _assert_contiguous_exact_seconds(_typed_heartbeats(metering, bmi_id, meter), meter)

        # Allocation bills from the moment the host was confirmed, which is at or
        # before the moment the instance started running: the allocation meter's
        # first interval cannot start after the consumption meter's.
        allocation_start = _parse_time(allocation["data"]["usage"]["from"])
        consumption_start = _parse_time(consumption["data"]["usage"]["from"])
        assert allocation_start <= consumption_start, (
            f"allocation starts billing at {allocation_start}, after consumption "
            f"starts at {consumption_start}: the host reservation must bill first"
        )
    finally:
        deletion_recorded = _delete_bmi(
            jwt_cli_user, jwt_grpc_tenant1, k8s_hub_client, uuid=bmi_id, cr_name=bmi_cr_name
        )

    assert deletion_recorded is not None, "the instance's CR was never deleted, so no deletion timestamp was recorded"

    # Each meter ends at the recorded deletion: its final partial interval is
    # billed, not dropped, and its typed heartbeats collapse into contiguous
    # net intervals billing exactly its length. The billing end is the latest
    # final heartbeat's own interval end: a cluster-recorded time from the
    # metering pipeline, because the runner's wall clock skews against the
    # pods and cannot anchor it.
    billing_end: datetime | None = None
    for meter in ("allocation", "consumption"):
        final = poll_until(
            fn=lambda meter=meter: _final_heartbeat(metering, bmi_id, deletion_recorded, meter=meter),
            until=lambda event: event is not None,
            retries=HEARTBEAT_TIMEOUT // 10,
            delay=10,
            description=f"final partial-interval {meter} heartbeat of {bmi_id}",
        )
        _, final_to, _ = _usage_interval(final)
        if billing_end is None or final_to > billing_end:
            billing_end = final_to

        net = _net_intervals(_typed_heartbeats(metering, bmi_id, meter))
        assert net, f"no typed {meter} heartbeats of {bmi_id} to assert the billed life on"
        for start, end, quantity in net:
            assert quantity == _interval_seconds(start, end), (
                f"{meter} net interval {start}..{end} bills {quantity}, not its exact length in seconds"
            )
        for (_, previous_end, _), (start, _, _) in itertools.pairwise(net):
            assert start == previous_end, f"{meter} billed intervals are not contiguous: {previous_end} then {start}"
        assert net[-1][1] >= deletion_recorded - CR_STAMP_TOLERANCE, (
            f"the final {meter} interval ends at {net[-1][1]}, before the recorded "
            f"deletion at {deletion_recorded}: the partial final interval was dropped"
        )

    # Nothing bills past the billing end, on either meter: a round that keeps
    # billing past it lands past this anchor, and a correction that follows
    # cancels it, so the poll passes only once both meters stop or take their
    # over-bill back.
    poll_until(
        fn=lambda: net_usage_past_by_dimension(metering.get_all_events(HEARTBEAT_TYPE, bmi_id), past=billing_end),
        until=lambda nets: all(abs(net) <= 1 for net in nets.values()),
        retries=HEARTBEAT_TIMEOUT // 10,
        delay=10,
        description=f"net billing of {bmi_id} to end at {billing_end}",
    )

    # Past the final interval nothing more arrives: both meters stopped.
    metering.assert_no_events(HEARTBEAT_TYPE, bmi_id, since=billing_end.isoformat(), within=180)


def _delete_bmi(cli: OsacCLI, grpc: GRPCClient, k8s: K8sClient, *, uuid: str, cr_name: str | None) -> datetime | None:
    """Delete the instance and return the deletion time the API server recorded.

    The deletion timestamp is read through a watch established before the
    delete request: the watch always delivers the modification that stamps
    it, where a plain get can arrive after the object is already gone. None
    when the instance never had a CR to watch: the cleanup path after a
    failed setup.
    """
    deletion_recorded: datetime | None = None
    if cr_name is None:
        with contextlib.suppress(Exception):
            cli.delete_baremetal_instance(uuid=uuid)
    else:
        with k8s.watch_deletion_timestamp(resource="baremetalinstance", name=cr_name) as deletion_stamp:
            with contextlib.suppress(Exception):
                cli.delete_baremetal_instance(uuid=uuid)
            with contextlib.suppress(Exception):
                deletion_recorded = _parse_time(
                    poll_until(
                        fn=deletion_stamp,
                        until=lambda stamp: bool(stamp),
                        retries=30,
                        delay=2,
                        description=f"deletion timestamp of {cr_name}",
                    )
                )
        with contextlib.suppress(Exception):
            wait_for_bmi_deletion(k8s=k8s, name=cr_name)
    with contextlib.suppress(Exception):
        wait_for_bmi_grpc_removal(grpc=grpc, uuid=uuid)
    return deletion_recorded
