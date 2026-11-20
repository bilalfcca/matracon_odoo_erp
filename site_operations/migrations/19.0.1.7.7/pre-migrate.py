"""
v1.7.7 pre-migration fixes:

1. Presence log: convert from event-log to session-log schema.
   Old schema: timestamp (NOT NULL), status (NOT NULL)
   New schema: online_at, offline_at  (both nullable from DB perspective)
   The table is expected to be empty on dev builds; on staging/production the
   old event rows are not meaningful, so we clear them for a clean start.

2. x.subcontractor.ipc.ipc_number: fill NULL values with 0 so Odoo can
   apply the NOT NULL constraint introduced in v1.7.6. Existing IPC records
   created before this field existed carry NULL; they get set to 0 here and
   should be updated manually to the correct sequential number afterwards.
"""


def migrate(cr, version):
    cr.execute("""
        DO $$
        BEGIN
            -- Remove NOT NULL from old columns so new INSERTs (which don't
            -- include these columns) succeed without constraint errors.
            IF EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'x_employee_presence_log'
                  AND column_name = 'timestamp'
                  AND is_nullable = 'NO'
            ) THEN
                ALTER TABLE x_employee_presence_log
                    ALTER COLUMN timestamp DROP NOT NULL;
            END IF;

            IF EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'x_employee_presence_log'
                  AND column_name = 'status'
                  AND is_nullable = 'NO'
            ) THEN
                ALTER TABLE x_employee_presence_log
                    ALTER COLUMN status DROP NOT NULL;
            END IF;

            -- Clear all old event-log rows; fresh start with session model.
            -- Guard: if the table was never created (first-time install of this
            -- feature) the DELETE would raise UndefinedTable, so check first.
            IF EXISTS (
                SELECT 1 FROM information_schema.tables
                WHERE table_name = 'x_employee_presence_log'
            ) THEN
                DELETE FROM x_employee_presence_log;
            END IF;
        END $$;
    """)

    # Fix 2: fill NULL ipc_number so the NOT NULL constraint can be enforced.
    # Guard: on a fresh install the table/column may not exist yet (the ORM
    # creates it after pre-migrate runs), so we skip safely in that case.
    cr.execute("""
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name  = 'x_subcontractor_ipc'
                  AND column_name = 'ipc_number'
            ) THEN
                UPDATE x_subcontractor_ipc
                SET ipc_number = 0
                WHERE ipc_number IS NULL;
            END IF;
        END $$;
    """)
