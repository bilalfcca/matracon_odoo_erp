"""Recalculate petty cash Balance Before / After in transaction-date order
(they were snapshots of the fund balance at posting time). Display fields
only - no accounting record is changed.

Memory-safe: one fund at a time (ledger and expenses read in SQL), cache
cleared after each fund."""
import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    cr.execute("SELECT id FROM x_petty_cash_fund ORDER BY id")
    fund_ids = [r[0] for r in cr.fetchall()]
    env = api.Environment(cr, SUPERUSER_ID, {})
    for fund_id in fund_ids:
        env['x.petty.cash.fund'].browse(fund_id)._mtr_recompute_running_balances()
        env.flush_all()
        env.invalidate_all()
    _logger.info("Petty cash: running balances recalculated for %s fund(s)", len(fund_ids))
