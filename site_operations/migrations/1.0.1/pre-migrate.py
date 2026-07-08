"""
Migration 1.0 → 1.0.1

Adds new columns to x_liability_sheet:
  - pm_id           (Many2one res.users)
  - pm_is_signed    (Boolean, default False)
  - pm_signature_date (Datetime)
  - pm_signed_sheet   (Binary, was already there but ensure column exists)
  - pm_signed_sheet_filename (Char)

Using pre-migrate so columns exist before the ORM sync runs.
"""


def migrate(cr, version):
    cr.execute("""
        ALTER TABLE x_liability_sheet
            ADD COLUMN IF NOT EXISTS pm_id INTEGER REFERENCES res_users(id) ON DELETE SET NULL,
            ADD COLUMN IF NOT EXISTS pm_is_signed BOOLEAN NOT NULL DEFAULT FALSE,
            ADD COLUMN IF NOT EXISTS pm_signature_date TIMESTAMP WITHOUT TIME ZONE,
            ADD COLUMN IF NOT EXISTS pm_signed_sheet BYTEA,
            ADD COLUMN IF NOT EXISTS pm_signed_sheet_filename VARCHAR;
    """)
