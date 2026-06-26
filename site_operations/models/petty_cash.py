from markupsafe import Markup

from odoo import models, fields, api, _
from odoo.exceptions import UserError

from . import matracon_notifications as matracon_notify


class PettyCashFund(models.Model):
    _name = 'x.petty.cash.fund'
    _description = 'Site Petty Cash Fund'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'project_analytic_account_id'

    name = fields.Char(compute='_compute_name', store=True, readonly=True)
    project_analytic_account_id = fields.Many2one(
        'account.analytic.account', string='Site Project',
        required=True, tracking=True, index=True)
    balance = fields.Monetary(
        compute='_compute_balance', store=True,
        currency_field='currency_id',
        help='Current petty cash balance after releases and expenses.',
    )
    total_released = fields.Monetary(
        compute='_compute_balance', store=True,
        currency_field='currency_id',
    )
    total_expensed = fields.Monetary(
        compute='_compute_balance', store=True,
        currency_field='currency_id',
    )
    currency_id = fields.Many2one(
        'res.currency', default=lambda self: self.env.company.currency_id)

    request_ids = fields.One2many(
        'x.petty.cash.request', 'fund_id', string='Requests')
    expense_ids = fields.One2many(
        'x.petty.cash.expense', 'fund_id', string='Expenses')

    @api.depends('project_analytic_account_id')
    def _compute_name(self):
        for fund in self:
            proj = fund.project_analytic_account_id.display_name or _('Petty Cash')
            fund.name = f'Petty Cash — {proj}'

    @api.depends(
        'request_ids.state', 'request_ids.released_amount',
        'expense_ids.amount', 'expense_ids.state',
    )
    def _compute_balance(self):
        for fund in self:
            released = sum(fund.request_ids.filtered(
                lambda r: r.state in ('released', 'confirmed')
            ).mapped('released_amount'))
            expensed = sum(fund.expense_ids.filtered(
                lambda e: e.state == 'posted'
            ).mapped('amount'))
            fund.total_released = released
            fund.total_expensed = expensed
            fund.balance = released - expensed

    @api.model
    def get_or_create_for_project(self, analytic):
        fund = self.search([
            ('project_analytic_account_id', '=', analytic.id),
        ], limit=1)
        if not fund:
            fund = self.create({'project_analytic_account_id': analytic.id})
        return fund


class PettyCashRequest(models.Model):
    _name = 'x.petty.cash.request'
    _description = 'Petty Cash Request'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'create_date desc'

    name = fields.Char(
        string='Reference', readonly=True, default=lambda self: _('New'))
    fund_id = fields.Many2one(
        'x.petty.cash.fund', string='Petty Cash Fund',
        required=True, ondelete='restrict', index=True)
    project_analytic_account_id = fields.Many2one(
        related='fund_id.project_analytic_account_id',
        store=True, readonly=True)
    current_balance = fields.Monetary(
        related='fund_id.balance', currency_field='currency_id', readonly=True)
    requested_amount = fields.Monetary(
        string='Required Amount', required=True, currency_field='currency_id')
    reason = fields.Text(required=True)
    state = fields.Selection([
        ('draft', 'Draft'),
        ('submitted', 'Submitted'),
        ('released', 'Released'),
        ('confirmed', 'Confirmed'),
        ('rejected', 'Rejected'),
    ], default='draft', tracking=True, required=True)
    released_amount = fields.Monetary(
        currency_field='currency_id', readonly=True, copy=False)
    payment_id = fields.Many2one(
        'account.payment', string='Release Payment', readonly=True, copy=False)
    currency_id = fields.Many2one(
        'res.currency', default=lambda self: self.env.company.currency_id)

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        user = self.env.user
        if 'fund_id' in fields_list and not res.get('fund_id'):
            analytic = getattr(user, 'x_default_analytic_account_id', False)
            if analytic:
                fund = self.env['x.petty.cash.fund'].get_or_create_for_project(analytic)
                res['fund_id'] = fund.id
        return res

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', _('New')) == _('New'):
                vals['name'] = self.env['ir.sequence'].next_by_code(
                    'x.petty.cash.request') or _('New')
            if not vals.get('fund_id') and vals.get('project_analytic_account_id'):
                fund = self.env['x.petty.cash.fund'].get_or_create_for_project(
                    self.env['account.analytic.account'].browse(
                        vals['project_analytic_account_id']))
                vals['fund_id'] = fund.id
        return super().create(vals_list)

    def action_submit(self):
        for req in self:
            if req.requested_amount <= 0:
                raise UserError(_('Enter a valid requested amount.'))
            req.state = 'submitted'
            req.message_post(body=_('Petty cash request submitted to Finance HO.'))
            fo_users = self.env['res.users'].search([
                ('group_ids', 'in', self.env.ref(
                    'site_operations.group_finance_ho').id),
            ])
            matracon_notify.notify_users(
                req, fo_users,
                _('Petty cash request <b>%s</b> — release required.') % req.name,
                summary=_('Petty Cash Request'),
            )
            matracon_notify.schedule_activity(
                req, fo_users, _('Release petty cash %s') % req.name)

    def action_release(self):
        """Finance HO releases petty cash via payment workflow."""
        self.ensure_one()
        if self.state != 'submitted':
            raise UserError(_('Only submitted requests can be released.'))
        Payment = self.env['account.payment']
        payment = Payment.create({
            'payment_type': 'outbound',
            'partner_type': 'supplier',
            'amount': self.requested_amount,
            'x_gross_approved_amount': self.requested_amount,
            'x_petty_cash_request_id': self.id,
            'x_payment_category': 'petty_cash',
            'x_destination_project_id': self.project_analytic_account_id.id,
        })
        self.payment_id = payment.id
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'account.payment',
            'view_mode': 'form',
            'res_id': payment.id,
            'context': {'default_x_petty_cash_request_id': self.id},
        }

    def action_confirm_receipt(self):
        for req in self:
            if req.state != 'released':
                raise UserError(_('Finance HO must release funds first.'))
            req.state = 'confirmed'
            req.message_post(
                body=Markup(_('Receipt confirmed by Site Accountant <b>%s</b>.'))
                % self.env.user.name)

    def action_mark_released(self, amount=None):
        """Called when FO payment is posted."""
        for req in self:
            req.released_amount = amount or req.requested_amount
            req.state = 'released'
            req.message_post(
                body=Markup(_('Petty cash <b>%s</b> released by Finance HO.'))
                % f'{req.released_amount:,.2f}')


class PettyCashExpense(models.Model):
    _name = 'x.petty.cash.expense'
    _description = 'Petty Cash Expense'
    _inherit = ['mail.thread']
    _order = 'expense_date desc, id desc'

    name = fields.Char(string='Description', required=True)
    fund_id = fields.Many2one(
        'x.petty.cash.fund', required=True, ondelete='restrict', index=True)
    project_analytic_account_id = fields.Many2one(
        related='fund_id.project_analytic_account_id', store=True, readonly=True)
    expense_date = fields.Date(
        default=fields.Date.context_today, required=True)
    amount = fields.Monetary(required=True, currency_field='currency_id')
    category = fields.Selection([
        ('travel', 'Travel'),
        ('supplies', 'Supplies'),
        ('utilities', 'Utilities'),
        ('meals', 'Meals'),
        ('other', 'Other'),
    ], default='other', required=True)
    receipt = fields.Binary(string='Receipt / Voucher')
    receipt_filename = fields.Char()
    state = fields.Selection([
        ('draft', 'Draft'),
        ('posted', 'Posted'),
    ], default='draft', tracking=True)
    currency_id = fields.Many2one(
        'res.currency', default=lambda self: self.env.company.currency_id)

    def action_post(self):
        for expense in self:
            if expense.amount <= 0:
                raise UserError(_('Expense amount must be positive.'))
            if expense.fund_id.balance < expense.amount - 0.01:
                raise UserError(_(
                    'Insufficient petty cash balance (available: %.2f).'
                ) % expense.fund_id.balance)
            expense.state = 'posted'
            expense.message_post(body=_('Expense posted against petty cash fund.'))
