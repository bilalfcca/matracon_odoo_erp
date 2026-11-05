def migrate(cr, version):
    """Recreate hr_employee_public VIEW to include x_project_analytic_account_id.

    The VIEW was created before HrEmployeePublicMatracon (which adds
    x_project_analytic_account_id) was properly ordered in hr_employee_ext.py.
    The ordering bug (commit 501e806) was fixed in code but existing production
    databases still have the old VIEW without the column.

    The record rule rule_hr_employee_site_accountant uses
    x_project_analytic_account_id in its domain.  Odoo redirects
    hr.employee.search_fetch() → hr.employee.public.search_fetch(), so the
    domain is applied against hr_employee_public which raises:
        UndefinedColumn: column hr_employee_public.x_project_analytic_account_id
                         does not exist

    Fix: call init() after the full registry is loaded so _get_fields()
    returns all current fields (including x_project_analytic_account_id) and
    recreates the view correctly.

    Safe to re-run: init() uses CREATE OR REPLACE VIEW.
    """
    from odoo import api, SUPERUSER_ID

    env = api.Environment(cr, SUPERUSER_ID, {})

    # Safety check: ensure the source column exists in hr_employee before
    # attempting to recreate the view (it always should, but guard defensively).
    cr.execute("""
        SELECT 1
        FROM information_schema.columns
        WHERE table_name = 'hr_employee'
          AND column_name = 'x_project_analytic_account_id'
    """)
    if not cr.fetchone():
        # Column missing from hr_employee — add it so the view can reference it.
        cr.execute("""
            ALTER TABLE hr_employee
            ADD COLUMN IF NOT EXISTS x_project_analytic_account_id INTEGER;
        """)

    # Recreate the view with all current fields.
    env['hr.employee.public'].init()
