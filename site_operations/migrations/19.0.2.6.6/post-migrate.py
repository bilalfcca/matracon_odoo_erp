"""Reconcile the partner pass-through pairs (Cr then Dr on the same payable
account, same partner/amount/label) of already-posted petty cash expense
entries, so they no longer show as outstanding payables. Accounts and amounts
are not changed."""
import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    moves = env['x.petty.cash.expense'].search([
        ('state', '=', 'posted'), ('x_account_move_id', '!=', False),
    ]).mapped('x_account_move_id').filtered(lambda m: m.state == 'posted')
    before = env['account.partial.reconcile'].search_count([])
    for i in range(0, len(moves), 200):
        env['x.petty.cash.expense']._mtr_reconcile_partner_pass_through(moves[i:i + 200])
    _logger.info("Petty cash: reconciled %s partner pass-through pair(s) in %s entries",
                 env['account.partial.reconcile'].search_count([]) - before, len(moves))
