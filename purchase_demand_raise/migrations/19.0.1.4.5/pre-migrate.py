def migrate(cr, version):
    """Ensure x_fulfillment_status column exists on purchase_order.

    This stored computed field was dropped from the dev branch and then
    restored (commit 783b1b3). Production may have received a code push
    where the field was present in Python but the DB column was never
    created (module not upgraded after the field was first added).

    Adding the column here (IF NOT EXISTS) is safe:
    - If the column is already there: no-op.
    - If it's missing: column is created so the ORM can read/write it
      before _compute_fulfillment_status recomputes all rows.
    """
    cr.execute("""
        ALTER TABLE purchase_order
        ADD COLUMN IF NOT EXISTS x_fulfillment_status VARCHAR
    """)
