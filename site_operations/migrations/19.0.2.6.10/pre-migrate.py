"""Fill the new journal-item Project column in SQL (fast) before the field
exists, so the upgrade does not recompute every journal line one by one.
Same rule as the compute: entry's Project (Site), else the line's first
analytic account."""


def migrate(cr, version):
    cr.execute("ALTER TABLE account_move_line ADD COLUMN IF NOT EXISTS x_project_id integer")
    cr.execute("""
        UPDATE account_move_line aml
           SET x_project_id = am.x_project_analytic_account_id
          FROM account_move am
         WHERE am.id = aml.move_id
           AND am.x_project_analytic_account_id IS NOT NULL
           AND aml.x_project_id IS NULL
    """)
    cr.execute("""
        UPDATE account_move_line aml
           SET x_project_id = sub.analytic_id
          FROM (
                SELECT l.id,
                       (SELECT split_part(k, ',', 1)::int
                          FROM jsonb_object_keys(l.analytic_distribution) k
                         WHERE split_part(k, ',', 1) ~ '^[0-9]+$'
                         ORDER BY k LIMIT 1) AS analytic_id
                  FROM account_move_line l
                 WHERE l.x_project_id IS NULL AND l.analytic_distribution IS NOT NULL
               ) sub
         WHERE sub.id = aml.id
           AND sub.analytic_id IN (SELECT id FROM account_analytic_account)
    """)
