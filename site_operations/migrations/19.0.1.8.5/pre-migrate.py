"""
v1.8.5 pre-migration:

Ensure res_users.x_backdate_default (Date) column exists.

The v1.8.4 migration containing this same DDL was never executed on staging
because the staging DB was already at version 1.8.4 when the fix was pushed
(same version → Odoo.sh skips the upgrade → migration never ran → crash).

Bumping to 1.8.5 forces a module upgrade so this migration actually runs.
Using ADD COLUMN IF NOT EXISTS so it is idempotent on environments where
the column already exists.
"""


def migrate(cr, version):
    cr.execute("""
        ALTER TABLE res_users
        ADD COLUMN IF NOT EXISTS x_backdate_default date;
    """)
