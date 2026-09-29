def migrate(cr, version):
    """Re-link known account XML IDs to their existing DB records (raw SQL).

    This is the belt-and-suspenders companion to the <function> call in
    account_configuration_data.xml.  The <function> approach (ORM-based) covers
    the common upgrade path.  This pre-migrate script fires BEFORE the data
    file loads, which provides an additional safety layer for edge cases where
    the ORM call hasn't executed yet.

    Runs on any database upgrading FROM 19.0.2.1.0 → 19.0.2.1.1, including
    staging environments that were cloned from a development snapshot already at
    19.0.2.1.0 (the 19.0.2.1.0 pre-migrate.py was skipped in those cases).
    """
    accounts_to_link = [
        ('account_wht_payable', '252100'),
        ('account_retention_payable', '211200'),
    ]
    for xml_id, code in accounts_to_link:
        cr.execute(
            "SELECT id FROM account_account WHERE code_store::text LIKE %s LIMIT 1",
            (f'%"{code}"%',),
        )
        row = cr.fetchone()
        if not row:
            continue
        account_id = row[0]

        cr.execute(
            "DELETE FROM ir_model_data WHERE module = 'site_operations' AND name = %s",
            (xml_id,),
        )
        cr.execute(
            """
            INSERT INTO ir_model_data (name, module, model, res_id, noupdate)
            VALUES (%s, 'site_operations', 'account.account', %s, false)
            """,
            (xml_id, account_id),
        )
