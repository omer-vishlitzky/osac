#!/bin/bash
set -euo pipefail

# The prefix is interpolated into SQL as an unquoted identifier, so a dash
# would produce a syntax error (or worse, a split identifier). Fail loud.
if [[ "${INSTANCE_PREFIX}" == *-* ]]; then
    echo "ERROR: INSTANCE_PREFIX '${INSTANCE_PREFIX}' contains a dash. pgPrefix is interpolated into SQL identifiers unquoted; use underscores (e.g. osac_pr1)." >&2
    exit 1
fi

echo "Creating per-instance databases for prefix '${INSTANCE_PREFIX}'..."

SERVICE_DB="${INSTANCE_PREFIX}_service"
METERING_DB="${INSTANCE_PREFIX}_metering"

for db in "$SERVICE_DB" "$METERING_DB"; do
    echo "Ensuring role and database: ${db}"
    psql -v ON_ERROR_STOP=1 -d postgres <<SQL
DO \$\$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='${db}') THEN
    CREATE ROLE ${db} LOGIN;
    RAISE NOTICE 'Created role ${db}';
  ELSE
    RAISE NOTICE 'Role ${db} already exists';
  END IF;
END \$\$;

SELECT 'CREATE DATABASE ${db} OWNER ${db}'
  WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname='${db}')
\gexec
SQL
done

echo "Database initialization complete."
