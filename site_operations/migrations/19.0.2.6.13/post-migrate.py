"""Recalculate petty cash Balance Before / After in transaction-date order
(they were snapshots of the fund balance at posting time). Display fields
only - no accounting record is changed."""
from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    env['x.petty.cash.fund'].search([])._mtr_recompute_running_balances()
