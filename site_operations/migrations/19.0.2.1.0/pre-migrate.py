def migrate(cr, version):
    """Clean up stale Studio views that crash view validation.

    (The WHT / Retention Payable xmlid linking that used to live here is
    gone — those accounts are now chosen on the Company form.)"""

    # Deactivate stale Studio view referencing a removed action
    cr.execute("""
        UPDATE ir_ui_view SET active = false
        WHERE name = 'account.move.vendor.bill.backcharge.section'
          AND active = true
    """)
    cr.execute("""
        DELETE FROM ir_model_data
        WHERE model = 'ir.ui.view'
          AND res_id IN (
              SELECT id FROM ir_ui_view
              WHERE name = 'account.move.vendor.bill.backcharge.section'
          )
    """)
    cr.execute("""
        UPDATE ir_ui_view SET arch_db = NULL
        WHERE name IN ('account.account.form.site.ops', 'account.account.list.site.ops')
    """)
