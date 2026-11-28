#!/usr/bin/env python3
"""Check that the per-run database URLs carry the pgx pool cap.

workload-db-secrets.yaml renders <run id>-db-config and
<run id>-db-metering-config in each CI run's vcluster. Those URLs are the
only ones the run's services use, so without pool_max_conns every run
opens three unbounded pgx pools (pgxpool defaults to max(4, NumCPU) per
pool) against the PostgreSQL server shared with every other run. This
renders the chart as a run does and checks both Secrets carry
dbInit.poolMaxConns, that the value drives the rendered number, and that
the rest of the URL survives.

Run: uv run --project .. python scripts/test-db-pool-cap-render.py
"""

from __future__ import annotations

import pathlib
import subprocess
import urllib.parse
from typing import Any

import yaml

INSTALLER = pathlib.Path(__file__).resolve().parents[1]
CHART = INSTALLER / "charts/osac"
# "**/" so the glob also matches when Helm treats one directory level as
# the chart-name/templates boundary; see test-chart-name-independence-render.py.
SHOW_ONLY = "**/workload-db-secrets.yaml"
RUN_ID = "pr2100v1"
DEFAULT_CAP = "8"

# Secret name -> (database name, client-cert mount used by that service).
EXPECTED = {
    f"{RUN_ID}-db-config": (f"{RUN_ID}_service", "/etc/fulfillment-grpc-server/db"),
    f"{RUN_ID}-db-metering-config": (f"{RUN_ID}_metering", "/etc/metering/db"),
}

# Query parameters every rendered URL must keep alongside the new cap.
REQUIRED_PARAMS = ("sslmode", "sslrootcert", "sslcert", "sslkey")


def render(*extra: str) -> list[dict[str, Any]]:
    cmd = [
        "helm",
        "template",
        "osac",
        str(CHART),
        "--values",
        str(CHART / "ci/default-values.yaml"),
        "--set",
        f"global.instanceId={RUN_ID}",
        "--show-only",
        SHOW_ONLY,
        *extra,
    ]
    manifest = subprocess.run(cmd, check=True, capture_output=True, text=True).stdout
    return [doc for doc in yaml.safe_load_all(manifest) if doc]


def urls_by_secret(docs: list[dict[str, Any]]) -> dict[str, str]:
    return {
        doc["metadata"]["name"]: doc["stringData"]["url"]
        for doc in docs
        if doc.get("kind") == "Secret" and doc.get("metadata", {}).get("name") in EXPECTED
    }


def check_url(secret_name: str, url: str, cap: str) -> None:
    database, cert_dir = EXPECTED[secret_name]
    parts = urllib.parse.urlsplit(url)
    assert parts.scheme == "postgres", f"{secret_name}: scheme is {parts.scheme!r}, not postgres"
    assert parts.port == 5432, f"{secret_name}: port is {parts.port!r}, not 5432"
    assert parts.path == f"/{database}", f"{secret_name}: database path is {parts.path!r}"

    params = urllib.parse.parse_qsl(parts.query)
    names = [name for name, _ in params]
    assert names.count("pool_max_conns") == 1, (
        f"{secret_name}: expected one pool_max_conns parameter, found {names.count('pool_max_conns')}"
    )
    values = dict(params)
    assert values["pool_max_conns"] == cap, (
        f"{secret_name}: pool_max_conns is {values['pool_max_conns']!r}, not {cap!r}"
    )
    for name in REQUIRED_PARAMS:
        assert name in values, f"{secret_name}: {name} disappeared from the URL"
    assert values["sslrootcert"] == f"{cert_dir}/sslrootcert", (
        f"{secret_name}: sslrootcert is {values['sslrootcert']!r}"
    )


def main() -> None:
    default_urls = urls_by_secret(render())
    assert set(default_urls) == set(EXPECTED), (
        f"expected the run's two database Secrets {sorted(EXPECTED)}, found {sorted(default_urls)}"
    )
    for name, url in default_urls.items():
        check_url(name, url, DEFAULT_CAP)

    custom_urls = urls_by_secret(render("--set", "dbInit.poolMaxConns=3"))
    assert set(custom_urls) == set(EXPECTED), (
        f"dbInit.poolMaxConns=3: expected {sorted(EXPECTED)}, found {sorted(custom_urls)}"
    )
    for name, url in custom_urls.items():
        check_url(name, url, "3")

    print("Database pool cap render checks passed.")
    for name in sorted(default_urls):
        print(f"  {name}: {default_urls[name]}")


if __name__ == "__main__":
    main()
