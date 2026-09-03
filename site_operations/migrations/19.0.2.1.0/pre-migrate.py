def migrate(cr, version):
    """Runs BEFORE any module data/views are loaded during this upgrade —
    cleans up known stale Studio-created database records that would
    otherwise crash view validation."""
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
