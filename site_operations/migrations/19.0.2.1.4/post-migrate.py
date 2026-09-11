def migrate(cr, version):
    """Remove the orphaned 'ipc_mob_amount' column from the Partner Ledger report.

    This column was added to account_report_column via the Odoo UI but has no
    corresponding SQL expression in the query, causing:
        UserError: The column 'ipc_mob_amount' is not available for this report.

    Deleting the record is safe: the column never returned data, so no historical
    report exports reference a real value from it.  The defensive code added to
    my_custom_module's _get_report_line_move_line will also prevent any similar
    orphaned columns from crashing the report in future.
    """
    cr.execute("""
        DELETE FROM account_report_column
        WHERE expression_label = 'ipc_mob_amount'
    """)
    deleted = cr.rowcount
    if deleted:
        import logging
        logging.getLogger(__name__).info(
            'Migration 19.0.2.1.4: removed %d orphaned ipc_mob_amount column '
            'record(s) from account_report_column.',
            deleted,
        )
