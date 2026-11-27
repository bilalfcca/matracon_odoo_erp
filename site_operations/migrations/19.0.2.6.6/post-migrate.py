"""Reconcile the partner pass-through pairs (Cr then Dr on the same payable
account, same partner/amount/label) of already-posted petty cash expense
entries, so they no longer show as outstanding payables. Accounts and amounts
are not changed.

Memory-safe: SQL first picks only the entries that actually contain an open
Cr/Dr pair (most entries have none), then they are reconciled in small
batches with the cache cleared after each batch.
"""
import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)
BATCH = 50


def migrate(cr, version):
    cr.execute("""
        SELECT DISTINCT am.id
          FROM x_petty_cash_expense e
          JOIN account_move am ON am.id = e.x_account_move_id AND am.state = 'posted'
          JOIN account_move_line c ON c.move_id = am.id AND c.credit > 0
          JOIN account_move_line d ON d.move_id = am.id AND d.debit > 0
          JOIN account_account a ON a.id = c.account_id
         WHERE e.state = 'posted'
           AND a.reconcile
           AND a.account_type IN ('liability_payable', 'asset_receivable')
           AND c.partner_id IS NOT NULL
           AND d.account_id = c.account_id
           AND d.partner_id = c.partner_id
           AND COALESCE(d.name, '') = COALESCE(c.name, '')
           AND ROUND(d.debit, 2) = ROUND(c.credit, 2)
           AND NOT COALESCE(c.reconciled, FALSE)
           AND NOT COALESCE(d.reconciled, FALSE)
      ORDER BY am.id
    """)
    move_ids = [r[0] for r in cr.fetchall()]
    env = api.Environment(cr, SUPERUSER_ID, {})
    cr.execute("SELECT COUNT(*) FROM account_partial_reconcile")
    before = cr.fetchone()[0]
    for i in range(0, len(move_ids), BATCH):
        env['x.petty.cash.expense']._mtr_reconcile_partner_pass_through(
            env['account.move'].browse(move_ids[i:i + BATCH]))
        env.flush_all()
        env.invalidate_all()
    cr.execute("SELECT COUNT(*) FROM account_partial_reconcile")
    _logger.info("Petty cash: reconciled %s partner pass-through pair(s) in %s entries",
                 cr.fetchone()[0] - before, len(move_ids))
