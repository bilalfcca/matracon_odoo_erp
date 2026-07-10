"""
v1.7.7 — Presence log: convert from event-log to session-log schema.

Old schema: timestamp (NOT NULL), status (NOT NULL)
New schema: online_at, offline_at  (both nullable from DB perspective)

The table is expected to be empty on dev builds; on staging/production the
old event rows are not meaningful, so we clear them for a clean start.
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
            DELETE FROM x_employee_presence_log;
        END $$;
    """)
