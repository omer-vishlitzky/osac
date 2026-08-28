from __future__ import annotations

import ipaddress
import logging
import os
from typing import Any
from uuid import uuid4

from tests.core.grpc_client import GRPCClient
from tests.core.helpers import wait_for_external_ip_allocated, wait_for_external_ip_cr
from tests.core.k8s_client import K8sClient
from tests.core.runner import env

logger = logging.getLogger(__name__)


def _external_ip_base() -> ipaddress.IPv4Network:
    """Per-slot /16 base network (OSAC_EXTERNAL_IP_BASE, e.g. 172.27.0.0/16).

    All worker-subnet math below is expressed relative to this base so
    concurrent pool slots never share address space.
    """
    base = ipaddress.ip_network(env("OSAC_EXTERNAL_IP_BASE"))
    if base.prefixlen != 16:
        raise RuntimeError(f"OSAC_EXTERNAL_IP_BASE must be a /16 network, got '{base}'")
    return base


def allocate_worker_subnet(prefix: int = 24) -> ipaddress.IPv4Network:
    """
    Allocate subnet with worker-based namespacing to prevent conflicts in parallel execution.

    Each pytest-xdist worker gets its own subdivision of the slot's base /16
    (OSAC_EXTERNAL_IP_BASE), split by the third octet:
    - Worker 0 (gw0): third octets   0-31 (/24) or 128-159 (/30)
    - Worker 1 (gw1): third octets  32-63 (/24) or 160-191 (/30)
    - Worker 2 (gw2): third octets  64-95 (/24) or 192-223 (/30)
    - Worker 3 (gw3): third octets  96-127 (/24) or 224-255 (/30)

    Within each worker, a sequential counter ensures unique, deterministic CIDRs.
    """
    base = _external_ip_base()

    # Get pytest-xdist worker ID (e.g., "gw0", "gw1", etc.)
    worker_id = os.environ.get("PYTEST_XDIST_WORKER", "gw0")
    worker_num = int(worker_id.replace("gw", "")) if worker_id.startswith("gw") else 0

    # Use a sequential counter within this worker's address space
    if not hasattr(allocate_worker_subnet, "_counter"):
        allocate_worker_subnet._counter = 0

    counter = allocate_worker_subnet._counter
    allocate_worker_subnet._counter += 1

    # The /16's third-octet slices, e.g. 172.27.0.0/24, 172.27.1.0/24, ...
    third_octet_networks = list(base.subnets(new_prefix=24))

    if prefix == 24:
        # /24 blocks use the lower half of the base /16 (third octets 0-127)
        # Each worker gets 32 /24 blocks
        # Worker 0: <base>.0.0/24, <base>.1.0/24, ..., <base>.31.0/24
        # Worker 1: <base>.32.0/24, <base>.33.0/24, ..., <base>.63.0/24
        # Worker 2: <base>.64.0/24, <base>.65.0/24, ..., <base>.95.0/24
        # Worker 3: <base>.96.0/24, <base>.97.0/24, ..., <base>.127.0/24
        third_octet = worker_num * 32 + counter
        if third_octet > 127:
            raise RuntimeError(f"Worker {worker_id} exhausted /24 address space (counter={counter})")
        return third_octet_networks[third_octet]
    elif prefix == 30:
        # /30 blocks use the upper half of the base /16 (third octets 128-255)
        # to avoid overlap with /24 blocks
        # Each worker gets 32 third octets, each with 64 /30 blocks
        # Worker 0: <base>.128.x - <base>.159.x
        # Worker 1: <base>.160.x - <base>.191.x
        # Worker 2: <base>.192.x - <base>.223.x
        # Worker 3: <base>.224.x - <base>.255.x
        third_octet = 128 + worker_num * 32 + (counter // 64)
        fourth_octet = (counter % 64) * 4
        if third_octet > 255:
            raise RuntimeError(f"Worker {worker_id} exhausted /30 address space (counter={counter})")
        return list(third_octet_networks[third_octet].subnets(new_prefix=30))[fourth_octet // 4]
    else:
        raise NotImplementedError(f"Prefix /{prefix} not supported")


def pool_status(private_grpc: GRPCClient, pool_id: str) -> dict[str, Any]:
    pool = private_grpc.get_external_ip_pool(pool_id=pool_id)
    raw = pool["object"]["status"]
    return {
        "total": int(raw.get("total", 0)),
        "allocated": int(raw.get("allocated", 0)),
        "available": int(raw.get("available", 0)),
    }


def create_ip(
    grpc: GRPCClient, k8s: K8sClient, pool_id: str
) -> tuple[str, str]:
    ip_name: str = f"test-ip-{uuid4().hex[:8]}"
    ip_id: str = grpc.create_external_ip(name=ip_name, pool=pool_id)
    ip_cr_name: str = wait_for_external_ip_cr(k8s=k8s, uuid=ip_id)
    wait_for_external_ip_allocated(k8s=k8s, name=ip_cr_name)
    return ip_id, ip_cr_name


def delete_ip(grpc: GRPCClient, ip_id: str) -> None:
    grpc.delete_external_ip(external_ip_id=ip_id)
