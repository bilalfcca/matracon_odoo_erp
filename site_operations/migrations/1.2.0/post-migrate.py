def migrate(cr, version):
    cr.execute("""
        ALTER TABLE x_liability_sheet_line
            ADD COLUMN IF NOT EXISTS payment_id INTEGER
    """)
