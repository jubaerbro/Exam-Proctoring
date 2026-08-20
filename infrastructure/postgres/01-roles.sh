#!/bin/bash
# Creates the two database roles Sentinel needs, and installs the extensions
# the schema depends on.
#
# Extensions are created HERE, as the superuser, and not by the migration.
# `CREATE EXTENSION` requires CREATE on the *database*, which the migrate role
# deliberately does not have — it owns the `public` schema, nothing more.
# Granting it CREATE on the database to work around that would hand the schema
# owner the ability to create new schemas outside the RLS generation loop in
# migration 0001, which is exactly the hole that loop exists to close. Managed
# Postgres (RDS, Cloud SQL, Azure) also refuses `CREATE EXTENSION` from a
# non-superuser regardless of grants, so a migration that installs its own
# extensions cannot be deployed there at all.
#
# The migration still says `CREATE EXTENSION IF NOT EXISTS`, which is a clean
# no-op once these exist — the privilege check is not reached.
#
# The separation is a security control, not tidiness:
#   sentinel_migrate  owns the schema and is used ONLY by Alembic
#   sentinel_app      owns nothing and is NOBYPASSRLS
#
# PostgreSQL silently ignores row-level security for a table's owner unless the
# table is FORCE'd, and always ignores it for superusers and BYPASSRLS roles.
# If the application connected as the owner or as a superuser, every RLS policy
# in this system would be decorative. See DATA_MODEL.md §1.
set -euo pipefail

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
    -- Extensions first: migration 0001's schema uses gen_random_uuid() in
    -- column defaults and citext for email, so they must exist before the
    -- migrate role connects.
    CREATE EXTENSION IF NOT EXISTS pgcrypto;
    CREATE EXTENSION IF NOT EXISTS citext;

    CREATE ROLE ${SENTINEL_MIGRATE_USER} LOGIN PASSWORD '${SENTINEL_MIGRATE_PASSWORD}';
    CREATE ROLE ${SENTINEL_APP_USER}     LOGIN PASSWORD '${SENTINEL_APP_PASSWORD}' NOBYPASSRLS;

    -- The migrate role owns the public schema and everything created in it.
    ALTER SCHEMA public OWNER TO ${SENTINEL_MIGRATE_USER};
    GRANT ALL ON SCHEMA public TO ${SENTINEL_MIGRATE_USER};

    -- The app role may use the schema but may not create in it.
    GRANT USAGE ON SCHEMA public TO ${SENTINEL_APP_USER};
    REVOKE CREATE ON SCHEMA public FROM PUBLIC;
    REVOKE CREATE ON SCHEMA public FROM ${SENTINEL_APP_USER};

    -- Default privileges so tables created later by migrate are usable by app
    -- without a further grant step. DELETE is granted because retention purging
    -- needs it; the append-only tables are protected by triggers, not by the
    -- absence of the privilege.
    ALTER DEFAULT PRIVILEGES FOR ROLE ${SENTINEL_MIGRATE_USER} IN SCHEMA public
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO ${SENTINEL_APP_USER};
    ALTER DEFAULT PRIVILEGES FOR ROLE ${SENTINEL_MIGRATE_USER} IN SCHEMA public
        GRANT USAGE, SELECT ON SEQUENCES TO ${SENTINEL_APP_USER};
    ALTER DEFAULT PRIVILEGES FOR ROLE ${SENTINEL_MIGRATE_USER} IN SCHEMA public
        GRANT EXECUTE ON FUNCTIONS TO ${SENTINEL_APP_USER};
EOSQL

echo "sentinel roles created: ${SENTINEL_MIGRATE_USER} (owner), ${SENTINEL_APP_USER} (NOBYPASSRLS)"
