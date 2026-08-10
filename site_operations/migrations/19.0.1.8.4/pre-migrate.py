"""
v1.8.4 pre-migration:

Add res_users.x_backdate_default (Date) if the column is missing.

The ORM declares this field in site_operations/models/res_users.py.
On Odoo.sh staging the column was not created during the initial deploy
(the module update did not complete), leaving every HTTP request crashing
with "column res_users.x_backdate_default does not exist".

We use ADD COLUMN IF NOT EXISTS so the script is safe to run multiple
times and on environments where the column already exists.
"""


def migrate(cr, version):
    cr.execute("""
        ALTER TABLE res_users
        ADD COLUMN IF NOT EXISTS x_backdate_default date;
    """)
