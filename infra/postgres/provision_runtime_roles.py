#!/usr/bin/env python3
"""Set M0-R02 runtime-role passwords through an explicit admin boundary.

Role definitions and grants are installed transactionally by migration 0002.
This one-shot operation contains no fallback to ``DATABASE_URL`` and prints no
credential or connection value.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from typing import Any
from urllib.parse import unquote, urlsplit

from rbac_contract import (
    ADMIN_DATABASE_ENV,
    COLUMN_SELECT_PRIVILEGES,
    COLUMN_UPDATE_PRIVILEGES,
    LEGACY_SHARED_ROLE,
    MIGRATION_METADATA_PRIVILEGES,
    PUBLIC_TABLES,
    ROLE_ATTRIBUTE_QUERY,
    ROLE_MEMBERSHIP_QUERY,
    ROLE_NAMES,
    RUNTIME_ROLES,
    SCHEMA_USAGE_PRIVILEGES,
    STATE_METADATA_PRIVILEGES,
    TABLE_PRIVILEGES,
    expected_role_attributes,
    missing_positive_path_requirements,
    missing_positive_schema_requirements,
    password_values,
)


class ProvisioningError(RuntimeError):
    """Raised when the installed role/grant contract is unsafe or incomplete."""


def admin_database_url(environment: Mapping[str, str]) -> str:
    value = environment.get(ADMIN_DATABASE_ENV, "")
    if not value.strip():
        raise ProvisioningError(
            f"{ADMIN_DATABASE_ENV} must be set explicitly; DATABASE_URL is intentionally ignored"
        )
    return value


def _single_boolean(connection: Any, query: str, parameters: tuple[Any, ...]) -> bool:
    row = connection.execute(query, parameters).fetchone()
    if row is None or len(row) != 1:
        raise ProvisioningError("PostgreSQL privilege evidence was incomplete")
    return bool(row[0])


def validate_admin_password_separation(
    database_url: str, runtime_passwords: Mapping[str, str]
) -> None:
    parsed = urlsplit(database_url)
    if (
        parsed.scheme not in {"postgres", "postgresql"}
        or not parsed.username
        or parsed.password is None
    ):
        raise ProvisioningError(
            f"{ADMIN_DATABASE_ENV} must be an explicit PostgreSQL URL with credentials"
        )
    admin_password = unquote(parsed.password)
    if admin_password in runtime_passwords.values():
        raise ProvisioningError(
            "PostgreSQL administrative and runtime-role passwords must be distinct"
        )


def validate_installed_contract(connection: Any) -> None:
    role_rows = tuple(tuple(row) for row in connection.execute(
        ROLE_ATTRIBUTE_QUERY, (list(ROLE_NAMES),)
    ).fetchall())
    if role_rows != expected_role_attributes():
        raise ProvisioningError("Runtime-role attributes do not match the M0-R02 contract")

    memberships = connection.execute(
        ROLE_MEMBERSHIP_QUERY, (list(ROLE_NAMES), list(ROLE_NAMES))
    ).fetchall()
    if memberships:
        raise ProvisioningError("Runtime roles must not inherit authority through memberships")

    legacy = connection.execute(
        "SELECT rolcanlogin FROM pg_roles WHERE rolname = %s",
        (LEGACY_SHARED_ROLE,),
    ).fetchone()
    if legacy is None or bool(legacy[0]):
        raise ProvisioningError("Legacy shared runtime role is absent or can still log in")

    table_actions = ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER")
    for privilege in ("CONNECT", "TEMPORARY"):
        if _single_boolean(
            connection,
            "SELECT has_database_privilege(%s, current_database(), %s)",
            (LEGACY_SHARED_ROLE, privilege),
        ):
            raise ProvisioningError(
                f"Legacy shared runtime role retains {privilege} database authority"
            )
    if _single_boolean(
        connection,
        "SELECT has_schema_privilege(%s, 'public', 'CREATE')",
        (LEGACY_SHARED_ROLE,),
    ) or _single_boolean(
        connection,
        "SELECT has_function_privilege(%s, 'public.reject_immutable_mutation()', 'EXECUTE')",
        (LEGACY_SHARED_ROLE,),
    ):
        raise ProvisioningError("Legacy shared runtime role retains object-creation authority")
    for table in PUBLIC_TABLES:
        for action in table_actions:
            if _single_boolean(
                connection,
                "SELECT has_table_privilege(%s, %s, %s)",
                (LEGACY_SHARED_ROLE, f"public.{table}", action),
            ):
                raise ProvisioningError(
                    f"Legacy shared runtime role retains {action} on public.{table}"
                )

    for role in ROLE_NAMES:
        if not _single_boolean(
            connection,
            "SELECT has_database_privilege(%s, current_database(), 'CONNECT')",
            (role,),
        ):
            raise ProvisioningError(f"{role} lacks its required database connection authority")
        if _single_boolean(
            connection,
            "SELECT has_database_privilege(%s, current_database(), 'TEMPORARY')",
            (role,),
        ):
            raise ProvisioningError(f"{role} unexpectedly has temporary-object authority")
        if not _single_boolean(
            connection,
            "SELECT has_schema_privilege(%s, 'public', 'USAGE')",
            (role,),
        ) or _single_boolean(
            connection,
            "SELECT has_schema_privilege(%s, 'public', 'CREATE')",
            (role,),
        ):
            raise ProvisioningError(f"{role} has an invalid public-schema permission shape")

        for table in PUBLIC_TABLES:
            for action in table_actions:
                expected = (role, table, action) in TABLE_PRIVILEGES
                observed = _single_boolean(
                    connection,
                    "SELECT has_table_privilege(%s, %s, %s)",
                    (role, f"public.{table}", action),
                )
                if observed != expected:
                    raise ProvisioningError(
                        f"Unexpected {action} privilege shape for {role} on public.{table}"
                    )

    column_rows = connection.execute(
        """
        SELECT grantee, table_name, column_name
        FROM information_schema.column_privileges AS column_grant
        WHERE table_schema = 'public'
          AND privilege_type = 'UPDATE'
          AND grantee = ANY(%s)
        ORDER BY grantee, table_name, column_name
        """,
        (list(ROLE_NAMES),),
    ).fetchall()
    if frozenset(tuple(row) for row in column_rows) != COLUMN_UPDATE_PRIVILEGES:
        raise ProvisioningError("Column-update grants do not match the M0-R02 contract")

    select_column_rows = connection.execute(
        """
        SELECT grantee, table_name, column_name
        FROM information_schema.column_privileges AS column_grant
        WHERE table_schema = 'public'
          AND privilege_type = 'SELECT'
          AND grantee = ANY(%s)
          AND NOT EXISTS (
              SELECT 1
              FROM information_schema.table_privileges AS table_grant
              WHERE table_grant.table_schema = column_grant.table_schema
                AND table_grant.table_name = column_grant.table_name
                AND table_grant.grantee = column_grant.grantee
                AND table_grant.privilege_type = 'SELECT'
          )
        ORDER BY grantee, table_name, column_name
        """,
        (list(ROLE_NAMES),),
    ).fetchall()
    if frozenset(tuple(row) for row in select_column_rows) != COLUMN_SELECT_PRIVILEGES:
        raise ProvisioningError(
            "Column-select grants do not match the positive-path contract"
        )

    if missing_positive_path_requirements():
        raise ProvisioningError(
            "Runtime grants do not satisfy every required positive path"
        )
    if missing_positive_schema_requirements():
        raise ProvisioningError(
            "Runtime schema grants do not satisfy every required positive path"
        )

    for role in ROLE_NAMES:
        for schema in ("smartcoat_migrations", "smartcoat_state"):
            for privilege in ("USAGE", "CREATE"):
                expected = (role, schema, privilege) in SCHEMA_USAGE_PRIVILEGES
                observed = _single_boolean(
                    connection,
                    "SELECT has_schema_privilege(%s, %s, %s)",
                    (role, schema, privilege),
                )
                if observed != expected:
                    raise ProvisioningError(
                        f"Unexpected {privilege} privilege shape for {role} on {schema}"
                    )

    metadata_rows = connection.execute(
        """
        SELECT grantee, table_name, privilege_type
        FROM information_schema.table_privileges
        WHERE table_schema = 'smartcoat_migrations'
          AND grantee = ANY(%s)
        ORDER BY grantee, table_name, privilege_type
        """,
        (list(ROLE_NAMES),),
    ).fetchall()
    if frozenset(tuple(row) for row in metadata_rows) != MIGRATION_METADATA_PRIVILEGES:
        raise ProvisioningError("Migration-metadata grants do not match the read-only backup contract")

    state_rows = connection.execute(
        """
        SELECT grantee, table_name, privilege_type
        FROM information_schema.table_privileges
        WHERE table_schema = 'smartcoat_state'
          AND grantee = ANY(%s)
        ORDER BY grantee, table_name, privilege_type
        """,
        (list(ROLE_NAMES),),
    ).fetchall()
    if frozenset(tuple(row) for row in state_rows) != STATE_METADATA_PRIVILEGES:
        raise ProvisioningError(
            "State-metadata grants do not match the read-only backup contract"
        )


def provision(environment: Mapping[str, str]) -> None:
    database_url = admin_database_url(environment)
    try:
        passwords = password_values(environment)
    except ValueError as exc:
        raise ProvisioningError(str(exc)) from exc
    validate_admin_password_separation(database_url, passwords)

    try:
        import psycopg
        from psycopg import sql
    except ImportError as exc:  # pragma: no cover - container dependency boundary
        raise ProvisioningError("The existing psycopg runtime dependency is unavailable") from exc

    try:
        with psycopg.connect(database_url) as connection:
            authority = connection.execute(
                "SELECT rolsuper FROM pg_roles WHERE rolname = current_user"
            ).fetchone()
            if authority is None or not bool(authority[0]):
                raise ProvisioningError(
                    "Credential provisioner requires the explicit bootstrap superuser authority"
                )
            validate_installed_contract(connection)
            for role in RUNTIME_ROLES.values():
                connection.execute(
                    sql.SQL("ALTER ROLE {} PASSWORD {}").format(
                        sql.Identifier(role.name),
                        sql.Literal(passwords[role.name]),
                    )
                )
    except ProvisioningError:
        raise
    except Exception as exc:
        raise ProvisioningError(
            "PostgreSQL runtime-role provisioning failed; connection and credential details are withheld"
        ) from exc


def main() -> int:
    try:
        provision(os.environ)
    except ProvisioningError as exc:
        print(f"Runtime-role provisioning error: {exc}", file=sys.stderr)
        return 2
    print("Runtime-role provisioning complete: roles=4 credentials_updated=4")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
