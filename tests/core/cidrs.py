from __future__ import annotations

import ipaddress

from tests.core.runner import env

# Second octets this slot owns: [OSAC_TEST_CIDR_BASE, OSAC_TEST_CIDR_BASE+7].
# The pool server leases bases as 200+8k (slot k), so 8-wide windows of
# concurrent slots never overlap (docs/pool-env-contract.md, invariant 3).
_SLOT_WINDOW = 8


def vnet_cidr() -> str:
    """The run's primary VirtualNetwork CIDR, allocated by the pool server."""
    return env("OSAC_VNET_CIDR")


def subnet_cidr() -> str:
    """A /24 inside vnet_cidr(), allocated by the pool server."""
    return env("OSAC_SUBNET_CIDR")


def test_cidr(offset: int) -> str:
    """VirtualNetwork slice j of this slot's CIDR window: 10.<base+offset>.0.0/16."""
    return _slot_cidr(offset=offset, prefix=16, third_octet=0)


def test_subnet_cidr(offset: int, third_octet: int) -> str:
    """Subnet 10.<base+offset>.<third_octet>.0/24 inside test_cidr(offset)."""
    return _slot_cidr(offset=offset, prefix=24, third_octet=third_octet)


def _slot_cidr(*, offset: int, prefix: int, third_octet: int) -> str:
    base = int(env("OSAC_TEST_CIDR_BASE"))
    if not 0 <= offset < _SLOT_WINDOW:
        raise RuntimeError(
            f"CIDR offset {offset} escapes this slot's window: the second octet must stay "
            f"within [{base}, {base + _SLOT_WINDOW - 1}] (OSAC_TEST_CIDR_BASE={base})"
        )
    if not 0 <= third_octet <= 255:
        raise RuntimeError(f"Subnet third octet {third_octet} is outside 0-255")
    network = ipaddress.ip_network(f"10.{base + offset}.{third_octet}.0/{prefix}")
    supernet = ipaddress.ip_network(f"10.{base}.0.0/12", strict=False)
    if not network.subnet_of(supernet):
        raise RuntimeError(f"CIDR {network} escapes the /12 supernet slice {supernet} of OSAC_TEST_CIDR_BASE={base}")
    return str(network)
