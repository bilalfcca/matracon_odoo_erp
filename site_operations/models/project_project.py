"""Native project.project extended as the financial + access anchor for each site."""

from odoo import models, fields, api


class ProjectProjectMatracon(models.Model):
    _inherit = 'project.project'

    x_analytic_account_id = fields.Many2one(
        'account.analytic.account',
        string='Project Analytic Account',
        tracking=True,
        index=True,
        help='Single analytic account for all costs, payments, and reporting on this project.',
    )
    x_site_config_id = fields.Many2one(
        'x.project.site.config',
        string='Site Configuration',
        readonly=True,
        copy=False,
    )
    x_site_store_user_ids = fields.Many2many(
        'res.users',
        'project_site_store_user_rel',
        'project_id', 'user_id',
        string='Site Store Managers',
        help='Users who manage inventory and procurement for this project site.',
    )
    x_site_accountant_user_ids = fields.Many2many(
        'res.users',
        'project_site_accountant_user_rel',
        'project_id', 'user_id',
        string='Site Accountants',
        help='Users who manage liabilities, petty cash, and payroll for this project site.',
    )

    # ── Live financial metrics (fund pool model) ───────────────────────────────
    x_funds_received = fields.Monetary(
        string='Funds Received',
        compute='_compute_project_financials',
        currency_field='currency_id',
        help='Inbound client / HO fund receipts tagged to this project.',
    )
    x_total_spent = fields.Monetary(
        string='Total Spent (Paid)',
        compute='_compute_project_financials',
        currency_field='currency_id',
        help='Outbound payments allocated from this project fund pool.',
    )
    x_available_balance = fields.Monetary(
        string='Available Balance',
        compute='_compute_project_financials',
        search='_search_available_balance',
        currency_field='currency_id',
        help='Funds received minus amounts already paid out from this project.',
    )
    x_total_vendor_liability = fields.Monetary(
        string='Vendor Liabilities',
        compute='_compute_project_financials',
        currency_field='currency_id',
        help='Outstanding vendor payables tagged to this project.',
    )
    x_total_sub_liability = fields.Monetary(
        string='Subcontractor Liabilities',
        compute='_compute_project_financials',
        currency_field='currency_id',
        help='Outstanding subcontractor payables tagged to this project.',
    )
    # ── Contract & Completion Tracking ────────────────────────────────────────
    x_contract_value = fields.Monetary(
        string='Contract Value',
        currency_field='currency_id',
        tracking=True,
        help='Total value of the project contract with the client.',
    )
    x_billed_to_client = fields.Monetary(
        string='Billed to Client',
        currency_field='currency_id',
        tracking=True,
        help='Total amount invoiced to the client so far.',
    )
    x_work_completion_pct = fields.Float(
        string='Work Completion %',
        digits=(5, 1),
        tracking=True,
        help='Manual entry: percentage of physical work completed on site.',
    )
    x_financial_completion_pct = fields.Float(
        string='Financial Completion %',
        compute='_compute_financial_completion_pct',
        digits=(5, 1),
        store=True,
        help='Auto: Billed to Client ÷ Contract Value × 100.',
    )
    x_remaining_work_value = fields.Monetary(
        string='Remaining Work Value',
        compute='_compute_financial_completion_pct',
        currency_field='currency_id',
        store=True,
    )

    currency_id = fields.Many2one(
        'res.currency',
        compute='_compute_currency_id',
        store=False,
    )

    @api.depends('company_id')
    def _compute_currency_id(self):
        for project in self:
            project.currency_id = (
                project.company_id.currency_id
                or self.env.company.currency_id
            )

    @api.depends('x_contract_value', 'x_billed_to_client')
    def _compute_financial_completion_pct(self):
        for project in self:
            if project.x_contract_value:
                project.x_financial_completion_pct = (
                    project.x_billed_to_client / project.x_contract_value * 100.0
                )
                project.x_remaining_work_value = max(
                    project.x_contract_value - project.x_billed_to_client, 0.0
                )
            else:
                project.x_financial_completion_pct = 0.0
                project.x_remaining_work_value = 0.0

    @api.depends('x_analytic_account_id')
    def _compute_project_financials(self):
        Payment = self.env['account.payment']
        Allocation = self.env['x.payment.project.allocation']
        AML = self.env['account.move.line']

        for project in self:
            analytic = project.x_analytic_account_id
            if not analytic:
                project.update({
                    'x_funds_received': 0.0,
                    'x_total_spent': 0.0,
                    'x_available_balance': 0.0,
                    'x_total_vendor_liability': 0.0,
                    'x_total_sub_liability': 0.0,
                })
                continue

            metrics = project._get_fund_metrics(Payment, Allocation, AML)
            project.update(metrics)

    @api.model
    def _search_available_balance(self, operator, value):
        """Allow search filters on non-stored available balance (Odoo 19)."""
        if operator not in ('<', '<=', '>', '>=', '=', '!='):
            return []
        Payment = self.env['account.payment']
        Allocation = self.env['x.payment.project.allocation']
        AML = self.env['account.move.line']
        matching = []
        for project in self.search([('x_analytic_account_id', '!=', False)]):
            balance = project._get_fund_metrics(
                Payment, Allocation, AML)['x_available_balance']
            if operator == '<' and balance < value:
                matching.append(project.id)
            elif operator == '<=' and balance <= value:
                matching.append(project.id)
            elif operator == '>' and balance > value:
                matching.append(project.id)
            elif operator == '>=' and balance >= value:
                matching.append(project.id)
            elif operator == '=' and balance == value:
                matching.append(project.id)
            elif operator == '!=' and balance != value:
                matching.append(project.id)
        return [('id', 'in', matching)]

    def _get_fund_metrics(self, Payment, Allocation, AML):
        """Compute fund-pool metrics for one project."""
        self.ensure_one()
        analytic = self.x_analytic_account_id
        analytic_id = analytic.id

        # ── Funds IN: posted inbound payments tagged to this project ────────
        inbound = Payment.search([
            ('payment_type', '=', 'inbound'),
            ('state', 'in', ('in_process', 'paid', 'partial', 'posted')),
            ('x_fund_project_id', '=', analytic_id),
        ])
        funds_received = sum(inbound.mapped('amount'))

        # ── Funds OUT: posted outbound allocations from this source project ───
        allocations = Allocation.search([
            ('project_analytic_account_id', '=', analytic_id),
            ('payment_id.payment_type', '=', 'outbound'),
            ('payment_id.state', 'in', ('in_process', 'paid', 'partial', 'posted')),
        ])
        total_spent = sum(allocations.mapped('allocation_amount'))

        # Outbound payments with no allocation lines but single fund project
        outbound_direct = Payment.search([
            ('payment_type', '=', 'outbound'),
            ('state', 'in', ('in_process', 'paid', 'partial', 'posted')),
            ('x_fund_project_id', '=', analytic_id),
            ('x_allocation_ids', '=', False),
        ])
        total_spent += sum(outbound_direct.mapped('amount'))

        available = funds_received - total_spent

        # ── Liabilities: open liability sheet balances (primary) ───────────
        vendor_liability = 0.0
        sub_liability = 0.0
        open_sheets = self.env['x.liability.sheet'].search([
            ('project_analytic_account_id', '=', analytic_id),
            ('state', 'in', ('draft', 'submitted', 'approved')),
        ])
        for sheet in open_sheets:
            for line in sheet.line_ids:
                remaining = max(line.liability_amount - line.paid_amount, 0.0)
                if remaining <= 0:
                    continue
                partner = line.partner_id
                if partner and partner.category_id.filtered(
                    lambda c: 'subcontractor' in (c.name or '').lower()
                ):
                    sub_liability += remaining
                else:
                    vendor_liability += remaining

        # Fallback: unreconciled payable AML with analytic tag
        if not open_sheets:
            str_aid = str(analytic_id)
            payable_lines = AML.search([
                ('parent_state', 'in', ('in_process', 'paid', 'partial', 'posted')),
                ('account_id.account_type', '=', 'liability_payable'),
                ('reconciled', '=', False),
                ('analytic_distribution', '!=', False),
            ])
            for line in payable_lines:
                dist = line.analytic_distribution or {}
                if str_aid not in dist:
                    continue
                pct = dist[str_aid] / 100.0
                balance = (line.credit - line.debit) * pct
                if balance <= 0:
                    continue
                partner = line.partner_id
                if partner and partner.category_id.filtered(
                    lambda c: 'subcontractor' in (c.name or '').lower()
                ):
                    sub_liability += balance
                else:
                    vendor_liability += balance

        return {
            'x_funds_received': funds_received,
            'x_total_spent': total_spent,
            'x_available_balance': available,
            'x_total_vendor_liability': vendor_liability,
            'x_total_sub_liability': sub_liability,
        }

    @api.model
    def get_available_balance_for_analytic(self, analytic_account):
        """Used by payment fund allocation — available pool for a source project."""
        if not analytic_account:
            return 0.0
        project = self.search(
            [('x_analytic_account_id', '=', analytic_account.id)], limit=1)
        if project:
            project.invalidate_recordset(
                ['x_funds_received', 'x_total_spent', 'x_available_balance'])
            return project.x_available_balance
        # Fallback when project record not yet linked
        Payment = self.env['account.payment']
        Allocation = self.env['x.payment.project.allocation']
        aid = analytic_account.id
        funds_in = sum(Payment.search([
            ('payment_type', '=', 'inbound'),
            ('state', 'in', ('in_process', 'paid', 'partial', 'posted')),
            ('x_fund_project_id', '=', aid),
        ]).mapped('amount'))
        spent = sum(Allocation.search([
            ('project_analytic_account_id', '=', aid),
            ('payment_id.payment_type', '=', 'outbound'),
            ('payment_id.state', 'in', ('in_process', 'paid', 'partial', 'posted')),
        ]).mapped('allocation_amount'))
        spent += sum(Payment.search([
            ('payment_type', '=', 'outbound'),
            ('state', 'in', ('in_process', 'paid', 'partial', 'posted')),
            ('x_fund_project_id', '=', aid),
            ('x_allocation_ids', '=', False),
        ]).mapped('amount'))
        return funds_in - spent

    @api.model
    def sync_from_site_configs(self):
        """Create/link project.project for every site configuration (install/upgrade)."""
        Config = self.env['x.project.site.config']
        for config in Config.search([]):
            config._ensure_project_record()
