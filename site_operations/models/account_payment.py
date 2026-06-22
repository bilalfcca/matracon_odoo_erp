from odoo import models, fields, api, _
from odoo.exceptions import UserError


class AccountPaymentSiteOps(models.Model):
    _inherit = ['account.payment', 'x.analytic.distribution.mixin']

    x_payment_status = fields.Selection([
        ('draft', 'Draft'),
        ('in_process', 'In Process'),
        ('paid', 'Paid'),
    ], string='Payment Status', default='draft', tracking=True)

    # Project whose fund pool is affected (IN: receives funds, OUT: primary tag)
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
        help='Outbound: project/cost center for the vendor payment analytic tag.',
    )

    x_source_project_ids = fields.Many2many(
        'account.analytic.account',
        'payment_source_project_rel',
        'payment_id', 'project_id',
        string='Source Projects',
        help='Outbound: projects whose fund pools will be debited (see Fund Allocation).',
    )

    x_liability_sheet_id = fields.Many2one(
        'x.liability.sheet', string='Liability Sheet', tracking=True,
        domain=[('state', '=', 'approved')])

    x_total_liability = fields.Float(
        related='x_liability_sheet_id.total_liability',
        string='Total Liability', readonly=True)

    x_total_approved = fields.Float(
        related='x_liability_sheet_id.total_approved',
        string='Total Approved', readonly=True)

    x_vendor_bank_account_id = fields.Many2one(
        'res.partner.bank', string='Vendor Bank Account',
        domain="[('partner_id', '=', partner_id)]")

    x_allocation_ids = fields.One2many(
        'x.payment.project.allocation', 'payment_id',
        string='Fund Allocation')

    x_available_bank_balance = fields.Float(
        string='Available Bank Balance',
        compute='_compute_available_bank_balance', store=False)

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

    # ─────────────────────────────────────────────────────────────────────────
    # ONCHANGE
    # ─────────────────────────────────────────────────────────────────────────

    @api.onchange('x_source_project_ids')
    def _onchange_source_projects_sync_allocations(self):
        """Keep fund allocation lines in sync with selected source projects."""
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

    # ─────────────────────────────────────────────────────────────────────────
    # VALIDATION & POSTING
    # ─────────────────────────────────────────────────────────────────────────

    def _validate_fund_allocations(self):
        """Block outbound payments that exceed a source project available balance."""
        Project = self.env['project.project']
        for payment in self:
            if payment.payment_type != 'outbound' or payment.state == 'posted':
                continue
            if payment.x_allocation_ids:
                total_alloc = sum(payment.x_allocation_ids.mapped('allocation_amount'))
                if payment.amount and abs(total_alloc - payment.amount) > 0.02:
                    raise UserError(_(
                        'Fund allocation total (%(alloc).2f) must equal payment amount (%(pay).2f).'
                    ) % {'alloc': total_alloc, 'pay': payment.amount})
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

    def action_post(self):
        self._validate_fund_allocations()
        res = super().action_post()
        for payment in self.filtered(lambda p: p.state == 'posted'):
            payment._matracon_tag_payment_move_analytic()
        return res

    def _matracon_tag_payment_move_analytic(self):
        """Ensure posted payment move lines carry destination project analytic."""
        self.ensure_one()
        analytic = self.x_destination_project_id or self.x_fund_project_id
        if not analytic or not self.move_id:
            return
        dist = self._analytic_distribution_for_account(analytic)
        # Tag non-liquidity lines (payable / expense side)
        lines = self.move_id.line_ids.filtered(
            lambda l: l.account_id.account_type in (
                'liability_payable', 'expense', 'expense_direct_cost',
                'asset_receivable',
            )
        )
        if lines:
            lines.write({'analytic_distribution': dist})

    def _prepare_move_line_default_vals(self, write_off_line_vals=None, force_balance=None):
        """Inject analytic distribution into payment journal entry lines."""
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

    # ─────────────────────────────────────────────────────────────────────────
    # ACTIONS / WORKFLOW
    # ─────────────────────────────────────────────────────────────────────────

    def action_set_in_process(self):
        self.write({'x_payment_status': 'in_process'})
        self.message_post(body=_('Payment set to In Process.'))

    def action_mark_paid(self):
        self.write({'x_payment_status': 'paid'})
        self.message_post(body=_('Payment marked as Paid.'))
