import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    """Remove the orphaned 'ipc_mob_amount' column from the Partner Ledger report.

    This column was added to account_report_column via the Odoo UI but has no
    corresponding SQL expression in the query, causing:
        UserError: The column 'ipc_mob_amount' is not available for this report.

    The DELETE is scoped to the Partner Ledger report (external ID
    account_reports.partner_ledger_report) so that any future legitimate
    ipc_mob_amount column on a different report is not accidentally removed.

    The defensive code added to my_custom_module's _get_report_line_move_line
    will also prevent any similar orphaned columns from crashing the report.
    """
    # Resolve the Partner Ledger report id — if the external ID doesn't exist
    # (e.g. account_reports not installed) skip gracefully.
    cr.execute("""
        SELECT res_id FROM ir_model_data
        WHERE module = 'account_reports'
          AND name   = 'partner_ledger_report'
          AND model  = 'account.report'
        LIMIT 1
    """)
    row = cr.fetchone()
    if not row:
        _logger.warning(
            'Migration 19.0.2.1.4: account_reports.partner_ledger_report not '
            'found — skipping ipc_mob_amount cleanup.'
        )
        return

    partner_ledger_id = row[0]
    cr.execute("""
        DELETE FROM account_report_column
        WHERE expression_label = 'ipc_mob_amount'
          AND report_id = %s
    """, (partner_ledger_id,))
    deleted = cr.rowcount
    if deleted:
        _logger.info(
            'Migration 19.0.2.1.4: removed %d orphaned ipc_mob_amount column '
            'record(s) from the Partner Ledger report.',
            deleted,
        )
