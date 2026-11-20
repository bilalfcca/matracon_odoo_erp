def migrate(cr, version):
    cr.execute("""
        ALTER TABLE account_move
            ADD COLUMN IF NOT EXISTS x_source_picking_id INTEGER
    """)
