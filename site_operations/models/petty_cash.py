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
    is_ho_fund = fields.Boolean(
        string='Head Office Fund', default=False,
        help='HO-level petty cash not tied to any specific project.')
    project_analytic_account_id = fields.Many2one(
        'account.analytic.account', string='Site Project',
        tracking=True, index=True)
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

    @api.depends('project_analytic_account_id', 'is_ho_fund')
    def _compute_name(self):
        for fund in self:
            if fund.is_ho_fund and not fund.project_analytic_account_id:
                fund.name = 'Petty Cash — Head Office'
            else:
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

    @api.model
    def get_or_create_ho_fund(self):
        fund = self.search([('is_ho_fund', '=', True)], limit=1)
        if not fund:
            fund = self.create({'is_ho_fund': True})
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
        ('ceo_approved', 'CEO Approved'),
        ('released', 'Released'),
        ('confirmed', 'Confirmed'),
        ('rejected', 'Rejected'),
    ], default='draft', tracking=True, required=True)
    released_amount = fields.Monetary(
        currency_field='currency_id', readonly=True, copy=False)
    ceo_amount_type = fields.Selection([
        ('full', 'Full Amount'),
        ('pct_75', '75%'),
        ('pct_50', '50%'),
        ('pct_25', '25%'),
        ('manual', 'Manual'),
    ], string='Approve Amount As', default='full')
    ceo_approved_amount = fields.Monetary(
        string='CEO Approved Amount', currency_field='currency_id', copy=False)
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

    _CEO_AMOUNT_FACTORS = {
        'full': 1.0, 'pct_75': 0.75, 'pct_50': 0.50, 'pct_25': 0.25,
    }

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
            # Auto-fill CEO approved amount when type is not manual and amount not explicitly set
            amount_type = vals.get('ceo_amount_type', 'full')
            if amount_type != 'manual' and not vals.get('ceo_approved_amount'):
                factor = self._CEO_AMOUNT_FACTORS.get(amount_type, 1.0)
                vals['ceo_approved_amount'] = (vals.get('requested_amount') or 0.0) * factor
        return super().create(vals_list)

    @api.onchange('ceo_amount_type', 'requested_amount')
    def _onchange_ceo_amount_type(self):
        mapping = {
            'full': 1.0,
            'pct_75': 0.75,
            'pct_50': 0.50,
            'pct_25': 0.25,
        }
        if self.ceo_amount_type and self.ceo_amount_type != 'manual':
            self.ceo_approved_amount = (
                self.requested_amount * mapping[self.ceo_amount_type]
            )

    def action_ceo_approve(self):
        for req in self:
            if req.state != 'submitted':
                raise UserError(_('Only submitted requests can be CEO-approved.'))
            if not req.ceo_approved_amount or req.ceo_approved_amount <= 0:
                raise UserError(_('Set the CEO approved amount before approving.'))
            req.state = 'ceo_approved'
            req.message_post(
                body=Markup(_(
                    'CEO approved petty cash: <b>%s %.2f</b>'
                )) % (req.currency_id.symbol, req.ceo_approved_amount)
            )
            # Notify finance HO
            fo_users = self.env['res.users'].search([
                ('group_ids', 'in', self.env.ref('site_operations.group_finance_ho').id),
            ])
            matracon_notify.notify_users(
                req, fo_users,
                _('Petty cash <b>%s</b> CEO-approved — release required.') % req.name,
                summary=_('Petty Cash Approved'),
            )
            matracon_notify.schedule_activity(
                req, fo_users, _('Release petty cash %s') % req.name)

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
            ceo_users = self.env['res.users'].search([
                ('group_ids', 'in', self.env.ref('purchase_demand_raise.group_ceo_approval').id),
            ])
            matracon_notify.notify_users(
                req, ceo_users,
                _('Petty cash request <b>%s</b> — CEO approval required.') % req.name,
                summary=_('Petty Cash Request'),
            )
            matracon_notify.schedule_activity(
                req, ceo_users, _('Approve petty cash %s') % req.name)

    def action_release(self):
        """Finance HO releases petty cash via payment workflow."""
        self.ensure_one()
        if self.state != 'ceo_approved':
            raise UserError(_('Only CEO-approved requests can be released.'))
        Payment = self.env['account.payment']
        analytic = self.project_analytic_account_id
        payment = Payment.create({
            'payment_type': 'outbound',
            'partner_type': 'supplier',
            'amount': self.ceo_approved_amount or self.requested_amount,
            'x_gross_approved_amount': self.ceo_approved_amount or self.requested_amount,
            'x_petty_cash_request_id': self.id,
            'x_payment_category': 'petty_cash',
            'x_ceo_approval_state': 'approved',
            'x_destination_project_id': analytic.id if analytic else False,
        })
        self.payment_id = payment.id
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'account.payment',
            'view_mode': 'form',
            'res_id': payment.id,
            'context': {'default_x_petty_cash_request_id': self.id},
        }

    def action_view_fund(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Petty Cash Fund'),
            'res_model': 'x.petty.cash.fund',
            'view_mode': 'form',
            'res_id': self.fund_id.id,
            'target': 'new',
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
    available_balance = fields.Monetary(
        related='fund_id.balance', string='Available Balance',
        currency_field='currency_id', readonly=True,
    )
    category = fields.Selection([
        ('travel', 'Travel'),
        ('supplies', 'Supplies'),
        ('utilities', 'Utilities'),
        ('meals', 'Meals'),
        ('other', 'Other'),
    ], default='other', required=True)
    journal_id = fields.Many2one(
        'account.journal',
        string='GL Journal',
        domain=[('type', 'in', ('cash', 'bank', 'general'))],
        help='Journal used for the petty cash expense entry.',
    )
    receipt = fields.Binary(string='Receipt / Voucher')
    receipt_filename = fields.Char()
    state = fields.Selection([
        ('draft', 'Draft'),
        ('posted', 'Posted'),
    ], default='draft', tracking=True)
    currency_id = fields.Many2one(
        'res.currency', default=lambda self: self.env.company.currency_id)

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        if 'fund_id' in fields_list and not res.get('fund_id'):
            analytic = self.env.user.sudo().x_default_analytic_account_id
            if analytic:
                fund = self.env['x.petty.cash.fund'].sudo().search([
                    ('project_analytic_account_id', '=', analytic.id)
                ], limit=1)
                if fund:
                    res['fund_id'] = fund.id
        return res

    def action_post(self):
        for expense in self:
            if expense.amount <= 0:
                raise UserError(_('Expense amount must be positive.'))
            balance_before = expense.fund_id.balance
            if balance_before < expense.amount - 0.01:
                raise UserError(_(
                    'Insufficient petty cash balance (available: %s %.2f).'
                ) % (expense.currency_id.symbol, balance_before))
            expense.state = 'posted'
            expense._create_journal_entry()
            balance_after = balance_before - expense.amount

            # Human-readable chatter with category label and balances
            category_label = dict(
                expense._fields['category'].selection
            ).get(expense.category, expense.category)
            sym = expense.currency_id.symbol
            body = Markup(
                '<b>Expense Posted</b><br/>'
                '<b>Description:</b> {name}<br/>'
                '<b>Category:</b> {cat}<br/>'
                '<b>Amount:</b> {sym} {amount}<br/>'
                '<b>Balance Before:</b> {sym} {before}<br/>'
                '<b>Balance After:</b> {sym} {after}'
            ).format(
                name=expense.name,
                cat=category_label,
                sym=sym,
                amount=f'{expense.amount:,.2f}',
                before=f'{balance_before:,.2f}',
                after=f'{balance_after:,.2f}',
            )

            # Attach receipt to the chatter message if present
            attachment_ids = []
            if expense.receipt:
                attachment = self.env['ir.attachment'].sudo().create({
                    'name': expense.receipt_filename or 'receipt',
                    'datas': expense.receipt,
                    'res_model': 'x.petty.cash.expense',
                    'res_id': expense.id,
                    'mimetype': 'application/octet-stream',
                })
                attachment_ids = [attachment.id]

            expense.message_post(body=body, attachment_ids=attachment_ids)

    def _create_journal_entry(self):
        """Create debit Expense / credit Petty Cash journal entry."""
        self.ensure_one()
        # Find or create a 'Petty Cash' cash journal
        Journal = self.env['account.journal']
        cash_journal = self.journal_id or Journal.search([
            ('type', '=', 'cash'),
            ('company_id', '=', self.env.company.id),
        ], limit=1)
        if not cash_journal:
            return  # No cash journal configured — skip silently

        # Use the journal's default cash account as the credit (petty cash source).
        # Note: payment_credit_account_id was removed in Odoo 17; default_account_id
        # is the correct field for cash/bank journals.
        credit_account = cash_journal.default_account_id
        if not credit_account:
            return  # Cannot determine petty cash account

        # Map expense category to an expense account code
        CATEGORY_ACCOUNT_MAP = {
            'travel': '6270',      # Travel expenses
            'supplies': '6280',    # Office supplies
            'utilities': '6300',   # Utilities
            'meals': '6290',       # Meals & entertainment
            'other': '6290',       # General admin expenses
        }
        AccountObj = self.env['account.account']
        code = CATEGORY_ACCOUNT_MAP.get(self.category, '6290')
        debit_account = AccountObj.search([
            ('code', 'like', code),
            ('company_id', '=', self.env.company.id),
        ], limit=1)
        if not debit_account:
            # Fallback: use journal default account on debit side too
            debit_account = credit_account

        analytic_distribution = {}
        if self.project_analytic_account_id:
            analytic_distribution = {
                str(self.project_analytic_account_id.id): 100
            }

        move_vals = {
            'move_type': 'entry',
            'journal_id': cash_journal.id,
            'date': self.expense_date,
            'ref': _('Petty Cash Expense: %s') % self.name,
            'line_ids': [
                (0, 0, {
                    'name': self.name,
                    'account_id': debit_account.id,
                    'debit': self.amount,
                    'credit': 0.0,
                    'analytic_distribution': analytic_distribution or False,
                }),
                (0, 0, {
                    'name': _('Petty Cash Fund'),
                    'account_id': credit_account.id,
                    'debit': 0.0,
                    'credit': self.amount,
                }),
            ],
        }
        move = self.env['account.move'].create(move_vals)
        move.action_post()
