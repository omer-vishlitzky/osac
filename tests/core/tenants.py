from __future__ import annotations

from tests.core.runner import env

# Local-dev fallback for hand-deployed clusters. CI always sets OSAC_TENANT_BASE
# (docs/pool-env-contract.md); this default keeps local runs working unchanged.
_LOCAL_TENANT_BASE = "tenant"


def tenant_name(number: int) -> str:
    """Per-run tenant name: <OSAC_TENANT_BASE><n>-<OSAC_RUN_ID>, unique per slot."""
    base = env("OSAC_TENANT_BASE", _LOCAL_TENANT_BASE)
    run_id = env("OSAC_RUN_ID")
    return f"{base}{number}-{run_id}"


def tenant_admin(number: int) -> str:
    return f"{tenant_name(number)}_admin"


def tenant_user(number: int) -> str:
    return f"{tenant_name(number)}_user"


def jwt_users() -> list[tuple[str, str]]:
    """(username, tenant) pairs the JWT fixtures authenticate as."""
    return [
        (tenant_admin(1), tenant_name(1)),
        (tenant_user(1), tenant_name(1)),
        (tenant_admin(2), tenant_name(2)),
        (tenant_user(2), tenant_name(2)),
    ]
