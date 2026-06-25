from odoo import models, fields, api, _
from odoo.exceptions import UserError


class AccountPaymentSiteOps(models.Model):
    _inherit = ['account.payment', 'x.interproject.accounting.mixin']

    x_payment_status = fields.Selection([
        ('draft', 'Draft'),
        ('in_process', 'In Process'),
        ('paid', 'Paid'),
    ], string='Payment Status', default='draft', tracking=True)

    x_fund_project_id = fields.Many2one(
        'account.analytic.account',
        string='Fund Project',
        tracking=True,
        help='Inbound: project receiving client/HO funds. '
             'Outbound without allocations: project funding the payment.',
    )
    x_fund_project_project_id = fields.Many2one(
        'project.project',
        string='Fund Project (App)',
        compute='_compute_fund_project_project_id',
        store=False,
    )

    x_destination_project_id = fields.Many2one(
        'account.analytic.account',
        string='Destination Project',
        tracking=True,
        help='Project for which this vendor payment is being made.',
    )

    x_source_project_ids = fields.Many2many(
        'account.analytic.account',
        'payment_source_project_rel',
        'payment_id', 'project_id',
        string='Source Projects',
        help='Projects whose fund pools will be debited (see Fund Allocation).',
    )

    x_liability_sheet_id = fields.Many2one(
        'x.liability.sheet', string='Liability Sheet', tracking=True,
        domain=[('state', 'in', ('approved', 'paid'))])

    x_liability_sheet_line_id = fields.Many2one(
        'x.liability.sheet.line', string='Liability Line',
        readonly=True, copy=False)

    x_gross_approved_amount = fields.Monetary(
        string='CEO Approved (Gross)',
        currency_field='currency_id',
        readonly=True,
        help='Locked gross amount approved by CEO on the liability sheet.',
    )

    x_total_liability = fields.Float(
        related='x_liability_sheet_id.total_liability',
        string='Total Liability', readonly=True)

    x_total_approved = fields.Float(
        related='x_liability_sheet_id.total_approved',
        string='Total Approved', readonly=True)

    x_vendor_bank_account_id = fields.Many2one(
        'res.partner.bank', string='Vendor Bank Account',
        domain="[('partner_id', '=', partner_id)]")

    x_cheque_number = fields.Char(string='Cheque / Reference No.', tracking=True)

    x_wht_tax_id = fields.Many2one(
        'account.tax', string='Withholding Tax (WHT)',
        domain="[('type_tax_use', '=', 'purchase'), ('active', '=', True)]")
    x_retention_tax_id = fields.Many2one(
        'account.tax', string='Retention Tax',
        domain="[('type_tax_use', '=', 'purchase'), ('active', '=', True)]")
    x_other_tax_id = fields.Many2one(
        'account.tax', string='Other Tax',
        domain="[('type_tax_use', '=', 'purchase'), ('active', '=', True)]")

    x_wht_amount = fields.Monetary(
        string='WHT Amount', compute='_compute_tax_amounts', store=True,
        currency_field='currency_id')
    x_retention_amount = fields.Monetary(
        string='Retention Amount', compute='_compute_tax_amounts', store=True,
        currency_field='currency_id')
    x_other_tax_amount = fields.Monetary(
        string='Other Tax Amount', compute='_compute_tax_amounts', store=True,
        currency_field='currency_id')
    x_total_tax_amount = fields.Monetary(
        string='Total Taxes', compute='_compute_tax_amounts', store=True,
        currency_field='currency_id')
    x_net_payable = fields.Monetary(
        string='Net Payable', compute='_compute_tax_amounts', store=True,
        currency_field='currency_id')

    x_allocation_ids = fields.One2many(
        'x.payment.project.allocation', 'payment_id',
        string='Fund Allocation')

    x_available_bank_balance = fields.Float(
        string='Available Bank Balance',
        compute='_compute_available_bank_balance', store=False)

    x_interproject_move_ids = fields.Many2many(
        'account.move', 'payment_interproject_move_rel',
        'payment_id', 'move_id',
        string='Inter-Project Entries', readonly=True, copy=False)

    # ─────────────────────────────────────────────────────────────────────────
    # COMPUTE
    # ─────────────────────────────────────────────────────────────────────────

    @api.depends('x_fund_project_id')
    def _compute_fund_project_project_id(self):
        Project = self.env['project.project']
        for payment in self:
            if payment.x_fund_project_id:
                payment.x_fund_project_project_id = Project.search(
                    [('x_analytic_account_id', '=', payment.x_fund_project_id.id)],
                    limit=1,
                )
            else:
                payment.x_fund_project_project_id = False

    @api.depends('journal_id')
    def _compute_available_bank_balance(self):
        for payment in self:
            if payment.journal_id and payment.journal_id.default_account_id:
                domain = [
                    ('account_id', '=', payment.journal_id.default_account_id.id),
                    ('parent_state', '=', 'posted'),
                ]
                lines = self.env['account.move.line'].search(domain)
                balance = sum(lines.mapped('debit')) - sum(lines.mapped('credit'))
                payment.x_available_bank_balance = balance
            else:
                payment.x_available_bank_balance = 0.0

    def _matracon_tax_amount(self, tax, base_amount):
        if not tax or base_amount <= 0:
            return 0.0
        res = tax.compute_all(
            base_amount,
            currency=self.currency_id,
            quantity=1.0,
            partner=self.partner_id,
        )
        return abs(sum(t.get('amount', 0.0) for t in res.get('taxes', [])))

    @api.depends(
        'x_gross_approved_amount', 'amount',
        'x_wht_tax_id', 'x_retention_tax_id', 'x_other_tax_id',
    )
    def _compute_tax_amounts(self):
        for payment in self:
            base = payment.x_gross_approved_amount or payment.amount or 0.0
            wht = payment._matracon_tax_amount(payment.x_wht_tax_id, base)
            retention = payment._matracon_tax_amount(payment.x_retention_tax_id, base)
            other = payment._matracon_tax_amount(payment.x_other_tax_id, base)
            payment.x_wht_amount = wht
            payment.x_retention_amount = retention
            payment.x_other_tax_amount = other
            payment.x_total_tax_amount = wht + retention + other
            payment.x_net_payable = max(base - payment.x_total_tax_amount, 0.0)

    # ─────────────────────────────────────────────────────────────────────────
    # ONCHANGE
    # ─────────────────────────────────────────────────────────────────────────

    @api.onchange('x_source_project_ids')
    def _onchange_source_projects_sync_allocations(self):
        if self.payment_type != 'outbound':
            return
        existing = {
            a.project_analytic_account_id.id: a
            for a in self.x_allocation_ids
            if a.project_analytic_account_id
        }
        lines = []
        for analytic in self.x_source_project_ids:
            if analytic.id in existing:
                lines.append((4, existing[analytic.id].id))
            else:
                lines.append((0, 0, {
                    'project_analytic_account_id': analytic.id,
                    'allocation_amount': 0.0,
                }))
        self.x_allocation_ids = lines

    @api.onchange('x_liability_sheet_id')
    def _onchange_liability_sheet_project(self):
        if self.x_liability_sheet_id and self.x_liability_sheet_id.project_analytic_account_id:
            self.x_destination_project_id = (
                self.x_liability_sheet_id.project_analytic_account_id.id
            )

    @api.onchange(
        'x_wht_tax_id', 'x_retention_tax_id', 'x_other_tax_id',
        'x_gross_approved_amount',
    )
    def _onchange_taxes_set_net_amount(self):
        if self.x_liability_sheet_line_id and self.x_net_payable:
            self.amount = self.x_net_payable

    # ─────────────────────────────────────────────────────────────────────────
    # VALIDATION & POSTING
    # ─────────────────────────────────────────────────────────────────────────

    def _validate_liability_payment(self):
        for payment in self.filtered(
            lambda p: p.payment_type == 'outbound' and p.x_liability_sheet_line_id
        ):
            if payment.x_gross_approved_amount and payment.amount > (
                payment.x_gross_approved_amount + 0.01
            ):
                raise UserError(_(
                    'Payment amount cannot exceed CEO approved gross amount (%(gross).2f).'
                ) % {'gross': payment.x_gross_approved_amount})
            if not payment.x_destination_project_id:
                raise UserError(_('Destination Project is required.'))
            if not payment.x_source_project_ids:
                raise UserError(_('Select at least one Source Project.'))
            if not payment.journal_id:
                raise UserError(_('Source Bank / Payment Journal is required.'))
            if not payment.x_cheque_number:
                raise UserError(_('Cheque / Reference Number is required.'))
            if not payment.x_wht_tax_id:
                raise UserError(_('Withholding Tax (WHT) is required.'))
            if payment.x_allocation_ids:
                total_alloc = sum(payment.x_allocation_ids.mapped('allocation_amount'))
                if abs(total_alloc - payment.amount) > 0.02:
                    raise UserError(_(
                        'Fund allocation total (%(alloc).2f) must equal net payment '
                        'amount (%(pay).2f).'
                    ) % {'alloc': total_alloc, 'pay': payment.amount})

    def _validate_fund_allocations(self):
        Project = self.env['project.project']
        for payment in self:
            if payment.payment_type != 'outbound' or payment.state == 'posted':
                continue
            if payment.x_allocation_ids:
                for alloc in payment.x_allocation_ids:
                    if alloc.allocation_amount <= 0:
                        continue
                    available = Project.get_available_balance_for_analytic(
                        alloc.project_analytic_account_id)
                    if alloc.allocation_amount > available + 0.01:
                        raise UserError(_(
                            'Insufficient fund balance on project "%(proj)s".\n'
                            'Available: %(avail).2f — Requested: %(req).2f'
                        ) % {
                            'proj': alloc.project_analytic_account_id.name,
                            'avail': available,
                            'req': alloc.allocation_amount,
                        })
            elif payment.x_fund_project_id and payment.amount:
                available = Project.get_available_balance_for_analytic(
                    payment.x_fund_project_id)
                if payment.amount > available + 0.01:
                    raise UserError(_(
                        'Insufficient fund balance on project "%(proj)s".\n'
                        'Available: %(avail).2f — Payment: %(pay).2f'
                    ) % {
                        'proj': payment.x_fund_project_id.name,
                        'avail': available,
                        'pay': payment.amount,
                    })

    def _matracon_create_interproject_entries(self):
        for payment in self.filtered(lambda p: p.state == 'posted'):
            dest = payment.x_destination_project_id
            if not dest or not payment.x_allocation_ids:
                continue
            ref = _('Payment %s — %s') % (payment.name, payment.partner_id.name)
            moves = self.env['account.move']
            for alloc in payment.x_allocation_ids.filtered(
                lambda a: a.allocation_amount > 0
            ):
                src = alloc.project_analytic_account_id
                if src and src != dest:
                    move = payment._create_interproject_entry(
                        src, dest, alloc.allocation_amount, ref)
                    moves |= move
            if moves:
                payment.x_interproject_move_ids = [(6, 0, moves.ids)]

    def _matracon_update_liability_on_post(self):
        for payment in self.filtered(
            lambda p: p.state == 'posted' and p.x_liability_sheet_line_id
        ):
            line = payment.x_liability_sheet_line_id
            line.paid_amount = (line.paid_amount or 0.0) + payment.amount
            payment.x_payment_status = 'paid'
            if payment.x_liability_sheet_id:
                payment.x_liability_sheet_id.action_finalize_if_fully_paid()

    def action_post(self):
        for payment in self.filtered(lambda p: p.x_liability_sheet_line_id):
            payment.amount = payment.x_net_payable or payment.amount
        self._validate_liability_payment()
        self._validate_fund_allocations()
        res = super().action_post()
        for payment in self.filtered(lambda p: p.state == 'posted'):
            payment._matracon_tag_payment_move_analytic()
            payment._matracon_create_interproject_entries()
            payment._matracon_update_liability_on_post()
        return res

    def _matracon_tag_payment_move_analytic(self):
        self.ensure_one()
        analytic = self.x_destination_project_id or self.x_fund_project_id
        if not analytic or not self.move_id:
            return
        dist = self._analytic_distribution_for_account(analytic)
        lines = self.move_id.line_ids.filtered(
            lambda l: l.account_id.account_type in (
                'liability_payable', 'expense', 'expense_direct_cost',
                'asset_receivable',
            )
        )
        if lines:
            lines.write({'analytic_distribution': dist})

    def _prepare_move_line_default_vals(self, write_off_line_vals=None, force_balance=None):
        line_vals_list = super()._prepare_move_line_default_vals(
            write_off_line_vals=write_off_line_vals,
            force_balance=force_balance,
        )
        analytic = None
        if self.payment_type == 'inbound':
            analytic = self.x_fund_project_id
        elif self.payment_type == 'outbound':
            analytic = self.x_destination_project_id or self.x_fund_project_id
        if not analytic:
            return line_vals_list
        dist = self._analytic_distribution_for_account(analytic)
        for vals in line_vals_list:
            account = self.env['account.account'].browse(vals.get('account_id'))
            if account.account_type in (
                'liability_payable', 'expense', 'expense_direct_cost',
                'asset_receivable',
            ):
                vals['analytic_distribution'] = dist
        return line_vals_list

    def action_set_in_process(self):
        self.write({'x_payment_status': 'in_process'})
        self.message_post(body=_('Payment set to In Process.'))

    def action_mark_paid(self):
        self.write({'x_payment_status': 'paid'})
        self.message_post(body=_('Payment marked as Paid.'))

    def write(self, vals):
        res = super().write(vals)
        if 'amount' in vals:
            for payment in self.filtered(
                lambda p: p.x_liability_sheet_line_id and p.x_gross_approved_amount
            ):
                if payment.amount > payment.x_gross_approved_amount + 0.01:
                    raise UserError(_(
                        'Cannot exceed CEO approved amount of %.2f.'
                    ) % payment.x_gross_approved_amount)
        return res
