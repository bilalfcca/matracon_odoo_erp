"""Extensions to fleet.vehicle.log.services — categories, analytic, GL posting."""

import logging
from odoo import models, fields, api
from odoo.exceptions import UserError, ValidationError

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
        ('direct', 'Direct Cash (no system record)'),
    ], string='Payment Source',
        help='How this expense was paid.\n'
             '• Petty Cash — link the petty cash expense below; GL is auto-linked.\n'
             '• Vendor Bill — link the vendor bill below; GL is auto-linked.\n'
             '• Direct Cash — a standalone journal entry is created when you click Post GL.')
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

    # ── Onchange helpers ──────────────────────────────────────────────────────

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

    @api.onchange('x_petty_cash_expense_id', 'x_vendor_bill_id')
    def _onchange_payment_source_auto_link_gl(self):
        """Immediately reflect the GL link in the form UI when a source is selected."""
        if self.x_account_move_id:
            return  # Already linked — don't overwrite
        if self.x_petty_cash_expense_id:
            pc = self.x_petty_cash_expense_id
            if pc.x_account_move_id and pc.x_account_move_id.state == 'posted':
                self.x_account_move_id = pc.x_account_move_id
        elif self.x_vendor_bill_id and self.x_vendor_bill_id.state == 'posted':
            self.x_account_move_id = self.x_vendor_bill_id

    # ── write() override: persist auto-link when payment source is saved ─────

    def write(self, vals):
        result = super().write(vals)
        # Auto-link GL whenever a payment source field changes and GL is not yet linked
        if 'x_petty_cash_expense_id' in vals or 'x_vendor_bill_id' in vals:
            for rec in self:
                if rec.x_account_move_id:
                    continue  # Already linked — skip
                move_id = None
                if rec.x_petty_cash_expense_id:
                    pc = rec.x_petty_cash_expense_id
                    if pc.x_account_move_id and pc.x_account_move_id.state == 'posted':
                        move_id = pc.x_account_move_id.id
                elif rec.x_vendor_bill_id and rec.x_vendor_bill_id.state == 'posted':
                    move_id = rec.x_vendor_bill_id.id
                if move_id:
                    # Use super() directly to avoid recursion
                    super(FleetLogEntryExt, rec).write({'x_account_move_id': move_id})
        return result

    # ── Constraints ───────────────────────────────────────────────────────────

    @api.constrains('x_log_category', 'notes')
    def _check_other_notes_required(self):
        for rec in self:
            if rec.x_log_category == 'other' and not (rec.notes or '').strip():
                raise ValidationError(
                    'Description / Notes is required for "Other Consumable" log entries.')

    # ── GL Posting — 3-path logic ─────────────────────────────────────────────

    def action_post_gl(self):
        """
        Link or create a GL journal entry for this log entry.

        Path 1 — Petty Cash: link to the posted JE on the linked petty cash expense
                 (no new entry — the petty cash JE already debited the expense account).
        Path 2 — Vendor Bill: link directly to the posted vendor bill move
                 (no new entry — the bill already debited the expense account).
        Path 3 — Direct Cash: create a standalone JE using the category expense account
                 (Dr) and the fleet settings default credit account (Cr).
        """
        for rec in self:
            if rec.x_account_move_id:
                continue  # Already linked — skip silently

            if not rec.amount:
                raise UserError(
                    f'Log entry for {rec.vehicle_id.x_mppl_code or rec.vehicle_id.name} '
                    f'has zero cost. Please enter the amount first.')

            settings = self.env['x.fleet.settings']._get_singleton()

            # ── Path 1: Petty Cash ────────────────────────────────────────────
            if rec.x_payment_source == 'petty_cash' and rec.x_petty_cash_expense_id:
                pc = rec.x_petty_cash_expense_id
                if not pc.x_account_move_id:
                    raise UserError(
                        f'Petty cash expense "{getattr(pc, "name", pc.id)}" has no journal '
                        'entry yet. Post the petty cash expense first, then return here.')
                if pc.x_account_move_id.state != 'posted':
                    raise UserError(
                        f'Petty cash expense journal entry is not in Posted state. '
                        'Confirm the petty cash entry first.')
                rec.x_account_move_id = pc.x_account_move_id
                continue

            # ── Path 2: Vendor Bill ───────────────────────────────────────────
            if rec.x_payment_source == 'vendor_bill' and rec.x_vendor_bill_id:
                bill = rec.x_vendor_bill_id
                if bill.state != 'posted':
                    raise UserError(
                        f'Vendor bill "{bill.name}" is not in Posted state. '
                        'Confirm the vendor bill first.')
                rec.x_account_move_id = bill
                continue

            # ── Path 3: Direct Cash — create standalone JE ───────────────────
            expense_account = settings.get_account_for_category(rec.x_log_category)
            if not expense_account:
                raise UserError(
                    f'No GL expense account configured for category '
                    f'"{dict(LOG_CATEGORIES).get(rec.x_log_category)}". '
                    'Go to Fleet → Configuration → Fleet Settings and map the account.')

            credit_account = settings.account_log_credit_id
            if not credit_account:
                raise UserError(
                    'No default credit account configured for direct-cash log entry GL.\n\n'
                    'Options:\n'
                    '1. Go to Fleet → Configuration → Fleet Settings → set '
                    '"Log Entry — Default Credit Account".\n'
                    '2. Or link a Petty Cash Expense or Vendor Bill above — '
                    'the GL will be auto-linked without needing this account.')

            analytic_id = rec.x_analytic_account_id
            analytic_distribution = {str(analytic_id.id): 100} if analytic_id else {}

            journal = self.env['account.journal'].search(
                [('type', '=', 'general'), ('company_id', '=', self.env.company.id)], limit=1)
            if not journal:
                raise UserError(
                    'No Miscellaneous journal found. '
                    'Create a General-type journal in Accounting → Configuration → Journals.')

            cat_label = dict(LOG_CATEGORIES).get(rec.x_log_category, rec.x_log_category)
            ref = (
                f"Fleet Log — "
                f"{rec.vehicle_id.x_mppl_code or rec.vehicle_id.name} / {cat_label}"
            )
            line_name = f"{rec.vehicle_id.name} — {cat_label}"

            move_vals = {
                'move_type': 'entry',
                'date': rec.date or fields.Date.context_today(rec),
                'ref': ref,
                'journal_id': journal.id,
                'line_ids': [
                    (0, 0, {
                        'name': line_name,
                        'account_id': expense_account.id,
                        'debit': rec.amount,
                        'credit': 0.0,
                        'analytic_distribution': analytic_distribution,
                        'partner_id': rec.purchaser_id.id if rec.purchaser_id else False,
                    }),
                    (0, 0, {
                        'name': line_name,
                        'account_id': credit_account.id,
                        'debit': 0.0,
                        'credit': rec.amount,
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
            raise UserError('No journal entry linked to this log entry yet.')
        return {
            'type': 'ir.actions.act_window',
            'name': 'Journal Entry',
            'res_model': 'account.move',
            'res_id': self.x_account_move_id.id,
            'view_mode': 'form',
        }

    def action_unlink_gl(self):
        """Detach the GL link so it can be corrected (does NOT reverse the JE itself)."""
        for rec in self:
            if rec.x_account_move_id and rec.x_account_move_id.state == 'posted':
                raise UserError(
                    f'Cannot unlink posted journal entry {rec.x_account_move_id.name}. '
                    'Reset it to Draft in Accounting → Journal Entries first, then unlink here.')
            rec.x_account_move_id = False
