"""Migration for Report 14: Customer Profitability.

Places the Customer Profitability menu under Client Operations with the page
route ``/customer-profitability`` and the ``banknote`` icon. The current
database already has this menu as ID 38 (URL without a leading slash); the
migration updates that row in place so the ID and every grant survive, and
inserts a row only when the menu is missing. No business table is touched:
the report only reads existing sales, cost and return data.

Usage:
    python -m db.migrations.migrate_customer_profitability --up
    python -m db.migrations.migrate_customer_profitability --down
    python -m db.migrations.migrate_customer_profitability --status

Rollback safety:
    ``--down`` is a no-op so existing grants survive.
"""

from __future__ import annotations

from db.migrations.client_operations_menu import SubmenuSpec, migrate_submenu_up, run_cli, submenu_status

SPEC = SubmenuSpec(
    title="Customer Profitability",
    url="/customer-profitability",
    icon="banknote",
    match_titles=("customer profitability", "customer-profitability"),
    match_urls=("customer-profitability", "/customer-profitability", "customer_profitability",
                "/menus/customer-profitability"),
)


def migrate_up(conn=None) -> bool:
    """Normalize (or create) the Customer Profitability menu under Client Operations."""
    return migrate_submenu_up(SPEC, conn)


def migrate_down(conn=None) -> bool:
    """No-op rollback: the menu is kept so existing grants survive."""
    return True


def migration_status(conn=None):
    return submenu_status(SPEC, conn)


def main(argv=None) -> int:
    return run_cli(SPEC, argv)


if __name__ == "__main__":
    raise SystemExit(main())
