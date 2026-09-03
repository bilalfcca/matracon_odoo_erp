def migrate(cr, version):
    """Pre-link known account XML IDs to their existing database records
    BEFORE account_configuration_data.xml loads, so Odoo performs a safe
    UPDATE instead of a colliding CREATE (duplicate code) or an orphaned
    DELETE (FK violation on account_move_line). Also cleans up unrelated
    stale Studio views that crash view validation."""

    accounts_to_link = [
        ('account_wht_payable', '252100'),
        ('account_retention_payable', '211200'),
    ]
    for xml_id, code in accounts_to_link:
        cr.execute("SELECT id FROM account_account WHERE code_store::text LIKE %s LIMIT 1", (f'%"{code}"%',))
        row = cr.fetchone()
        if not row:
            continue
        account_id = row[0]
        cr.execute("""
            DELETE FROM ir_model_data
            WHERE module = 'site_operations' AND name = %s
        """, (xml_id,))
        cr.execute("""
            INSERT INTO ir_model_data (name, module, model, res_id, noupdate)
            VALUES (%s, 'site_operations', 'account.account', %s, false)
        """, (xml_id, account_id))

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
