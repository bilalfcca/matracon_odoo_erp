"""Extensions to fleet.vehicle.log.services — categories, analytic, GL posting."""

import logging
from odoo import models, fields, api
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

LOG_CATEGORIES = [
    ('fuel_diesel', 'Fuel — Diesel'),
    ('fuel_petrol', 'Fuel — Petrol'),
    ('oil_change', 'Oil Change'),
    ('air_filter', 'Air Filter'),
    ('oil_filter', 'Oil Filter'),
    ('ac_filter', 'AC Filter'),
    ('other', 'Other Consumable'),
]


class FleetLogEntryExt(models.Model):
    _inherit = 'fleet.vehicle.log.services'

    # ── Category ──────────────────────────────────────────────────────────────
    x_log_category = fields.Selection(
        LOG_CATEGORIES, string='Log Category', default='fuel_diesel',
        help='Type of fuel/consumable/service being recorded.')

    # ── Meter readings ────────────────────────────────────────────────────────
    x_prev_meter = fields.Float(string='Previous Meter', digits=(16, 2), readonly=True)
    x_curr_meter = fields.Float(string='Current Meter', digits=(16, 2))
    x_meter_delta = fields.Float(
        string='Consumption / Interval', digits=(16, 2),
        compute='_compute_meter_delta', store=True)
    x_meter_unit = fields.Selection(
        related='vehicle_id.x_meter_unit', string='Unit', readonly=True)

    # ── Cost breakdown ────────────────────────────────────────────────────────
    x_quantity = fields.Float(string='Quantity', digits=(16, 2), default=1.0)
    x_unit_cost = fields.Float(string='Unit Cost (PKR)', digits=(16, 2))
    # `amount` on the parent model stores total cost — we compute it from qty × unit cost

    # ── Site & Analytic ───────────────────────────────────────────────────────
    x_analytic_account_id = fields.Many2one(
        'account.analytic.account', string='Project Site',
        related='vehicle_id.x_analytic_account_id',
        store=True, readonly=True)

    # ── Driver (employee) ─────────────────────────────────────────────────────
    x_driver_employee_id = fields.Many2one(
        'hr.employee', string='Driver',
        help='Employee driver for this log entry.')

    # ── Payment source ────────────────────────────────────────────────────────
    x_payment_source = fields.Selection([
        ('petty_cash', 'Petty Cash'),
        ('vendor_bill', 'Vendor Credit / Bill'),
    ], string='Payment Source')
    x_petty_cash_expense_id = fields.Many2one(
        'x.petty.cash.expense', string='Petty Cash Expense',
        domain=[('state', '=', 'posted')])
    x_vendor_bill_id = fields.Many2one(
        'account.move', string='Vendor Bill',
        domain=[('move_type', '=', 'in_invoice'), ('state', '=', 'posted')])

    # ── GL Journal Entry ──────────────────────────────────────────────────────
    x_account_move_id = fields.Many2one(
        'account.move', string='Journal Entry', readonly=True, copy=False)
    x_gl_posted = fields.Boolean(
        string='GL Posted', compute='_compute_gl_posted', store=False)

    @api.depends('x_account_move_id', 'x_account_move_id.state')
    def _compute_gl_posted(self):
        for rec in self:
            rec.x_gl_posted = bool(
                rec.x_account_move_id and rec.x_account_move_id.state == 'posted')

    @api.depends('x_curr_meter', 'x_prev_meter')
    def _compute_meter_delta(self):
        for rec in self:
            rec.x_meter_delta = max(0.0, (rec.x_curr_meter or 0.0) - (rec.x_prev_meter or 0.0))

    @api.onchange('vehicle_id')
    def _onchange_vehicle_fill_prev_meter(self):
        """Auto-fill previous meter from last log entry."""
        if self.vehicle_id:
            last = self.env['fleet.vehicle.log.services'].search([
                ('vehicle_id', '=', self.vehicle_id.id),
                ('x_curr_meter', '>', 0),
                ('id', '!=', self._origin.id if self._origin else 0),
            ], order='date desc, id desc', limit=1)
            self.x_prev_meter = last.x_curr_meter if last else 0.0
            # Auto-fill driver from vehicle's assigned driver
            if self.vehicle_id.driver_id:
                employee = self.env['hr.employee'].search(
                    [('address_id', '=', self.vehicle_id.driver_id.id)], limit=1)
                if employee:
                    self.x_driver_employee_id = employee

    @api.onchange('x_quantity', 'x_unit_cost')
    def _onchange_compute_amount(self):
        if self.x_quantity and self.x_unit_cost:
            self.amount = self.x_quantity * self.x_unit_cost

    @api.onchange('x_log_category')
    def _onchange_log_category_payment_source(self):
        """Pre-fill payment source from fleet settings."""
        settings = self.env['x.fleet.settings']._get_singleton()
        if settings.fuel_payment_source != 'both':
            self.x_payment_source = (
                'petty_cash' if settings.fuel_payment_source == 'petty_cash' else 'vendor_bill'
            )

    def action_post_gl(self):
        """Create and post GL journal entry for this log entry."""
        for rec in self:
            if rec.x_account_move_id:
                raise UserError('A journal entry has already been created for this log entry.')
            if not rec.amount:
                raise UserError('Cannot post a log entry with zero cost.')

            settings = self.env['x.fleet.settings']._get_singleton()
            expense_account = settings.get_account_for_category(rec.x_log_category)
            if not expense_account:
                raise UserError(
                    f'No GL account configured for category '
                    f'"{dict(LOG_CATEGORIES).get(rec.x_log_category)}". '
                    'Please go to Fleet → Configuration → Fleet Settings and set the account.')

            # Determine credit account
            credit_account = None
            if rec.x_payment_source == 'petty_cash' and rec.x_petty_cash_expense_id:
                credit_account = rec.x_petty_cash_expense_id.x_petty_cash_account_id
            elif rec.x_payment_source == 'vendor_bill' and rec.x_vendor_bill_id:
                credit_account = (
                    rec.vendor_id.property_account_payable_id if rec.vendor_id else None
                )

            if not credit_account:
                raise UserError(
                    'Cannot post GL entry: no credit account found. '
                    'Link a petty cash expense or vendor bill first.')

            analytic_id = rec.x_analytic_account_id
            analytic_distribution = {str(analytic_id.id): 100} if analytic_id else {}

            move_vals = {
                'move_type': 'entry',
                'date': rec.date or fields.Date.context_today(rec),
                'ref': (
                    f"Fleet Log — {rec.vehicle_id.x_mppl_code or rec.vehicle_id.name} / "
                    f"{dict(LOG_CATEGORIES).get(rec.x_log_category, '')}"
                ),
                'journal_id': self.env['account.journal'].search(
                    [('type', '=', 'general')], limit=1).id,
                'line_ids': [
                    (0, 0, {
                        'name': (
                            f"{rec.vehicle_id.name} — "
                            f"{dict(LOG_CATEGORIES).get(rec.x_log_category, '')}"
                        ),
                        'account_id': expense_account.id,
                        'debit': rec.amount,
                        'credit': 0.0,
                        'analytic_distribution': analytic_distribution,
                        'partner_id': rec.purchaser_id.id if rec.purchaser_id else False,
                    }),
                    (0, 0, {
                        'name': (
                            f"{rec.vehicle_id.name} — "
                            f"{dict(LOG_CATEGORIES).get(rec.x_log_category, '')}"
                        ),
                        'account_id': credit_account.id,
                        'debit': 0.0,
                        'credit': rec.amount,
                        'analytic_distribution': analytic_distribution,
                        'partner_id': rec.purchaser_id.id if rec.purchaser_id else False,
                    }),
                ],
            }
            move = self.env['account.move'].create(move_vals)
            move.action_post()
            rec.x_account_move_id = move
        return True

    def action_view_journal_entry(self):
        self.ensure_one()
        if not self.x_account_move_id:
            raise UserError('No journal entry linked to this log entry.')
        return {
            'type': 'ir.actions.act_window',
            'name': 'Journal Entry',
            'res_model': 'account.move',
            'res_id': self.x_account_move_id.id,
            'view_mode': 'form',
        }
