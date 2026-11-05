def migrate(cr, version):
    """Add x_fleet_vehicle_id column to stock_picking if missing.

    This column was added in commit d209203 (Vehicle issue type for fuel/diesel)
    but was never applied to databases where `odoo-bin -u site_operations` was not
    run after that commit.  The module also lacked 'fleet' in its depends list,
    which prevented Odoo from creating the FK column automatically on environments
    where fleet was installed but not declared as a dependency.

    Runs on any database upgrading from 19.0.2.1.1 → 19.0.2.1.2.
    Safe to re-run: ADD COLUMN IF NOT EXISTS is idempotent.
    """
    cr.execute("""
        ALTER TABLE stock_picking
        ADD COLUMN IF NOT EXISTS x_fleet_vehicle_id INTEGER;
    """)
    # Add the FK constraint only if fleet_vehicle table exists and constraint is absent
    cr.execute("""
        SELECT 1
        FROM information_schema.table_constraints
        WHERE constraint_name = 'stock_picking_x_fleet_vehicle_id_fkey'
          AND table_name = 'stock_picking'
    """)
    has_fk = cr.fetchone()

    cr.execute("""
        SELECT 1 FROM information_schema.tables
        WHERE table_name = 'fleet_vehicle'
    """)
    has_fleet_table = cr.fetchone()

    if has_fleet_table and not has_fk:
        cr.execute("""
            ALTER TABLE stock_picking
            ADD CONSTRAINT stock_picking_x_fleet_vehicle_id_fkey
            FOREIGN KEY (x_fleet_vehicle_id)
            REFERENCES fleet_vehicle(id)
            ON DELETE SET NULL;
        """)
