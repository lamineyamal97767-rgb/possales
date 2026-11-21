"""Migration for Report 13: Customer Payment Risk.

Places the Customer Payment Risk menu under Client Operations with the page
route ``/customer-payment-risk`` and the ``alert-triangle`` icon. The current
database already has this menu as ID 37 (with the URL missing its leading
slash); the migration updates that row in place so the ID and every grant in
``dashboard_auth.allowed_menus`` survive. It inserts a row only when the menu
is genuinely missing. No other menu row is changed and no business table is
touched: the report only reads existing customer, invoice and payment data.

Usage:
    python -m db.migrations.migrate_customer_payment_risk --up
    python -m db.migrations.migrate_customer_payment_risk --down
    python -m db.migrations.migrate_customer_payment_risk --status

Rollback safety:
    ``--down`` is a no-op. The menu pre-existed this feature and may carry
    grants, and there is no application-owned table to drop.
"""

from __future__ import annotations

from db.migrations.client_operations_menu import SubmenuSpec, migrate_submenu_up, run_cli, submenu_status

SPEC = SubmenuSpec(
    title="Customer Payment Risk",
    url="/customer-payment-risk",
    icon="alert-triangle",
    match_titles=("customer payment risk", "customer-payment-risk"),
    match_urls=("customer-payment-risk", "/customer-payment-risk", "customer_payment_risk",
                "/menus/customer-payment-risk"),
)


def migrate_up(conn=None) -> bool:
    """Normalize (or create) the Customer Payment Risk menu under Client Operations."""
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
