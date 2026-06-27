"""Unified management dashboard — CEO, FO, and Site Accountant."""

from datetime import timedelta

from odoo import models, fields, api, _
from odoo.exceptions import UserError


class ManagementDashboard(models.TransientModel):
    _name = 'x.management.dashboard'
    _description = 'Management Dashboard'

    name = fields.Char(readonly=True)
    user_name = fields.Char(readonly=True)

    # ── View & scope filters ─────────────────────────────────────────────────
    filter_dashboard_tab = fields.Selection([
        ('overview', 'Management Overview'),
        ('project', 'Project Dashboard'),
        ('bank_guarantee', 'Bank Guarantees'),
    ], string='Dashboard View', default='overview', required=True)

    filter_scope = fields.Selection([
        ('all', 'All Projects'),
        ('single', 'Single Project'),
    ], string='Project Scope', default='all', required=True)

    filter_project_id = fields.Many2one(
        'account.analytic.account', string='Project')
    available_project_ids = fields.Many2many(
        'account.analytic.account',
        'mgmt_dash_avail_proj_rel', 'dash_id', 'project_id',
        readonly=True,
    )

    filter_period_preset = fields.Selection([
        ('monthly', 'Monthly'),
        ('quarterly', 'Quarterly'),
        ('yearly', 'Yearly'),
        ('custom', 'Custom Dates'),
        ('all', 'All Time'),
    ], string='Period', default='yearly', required=True)

    filter_date_from = fields.Date(string='From Date')
    filter_date_to = fields.Date(string='To Date')

    # ── Overview KPIs ────────────────────────────────────────────────────────
    kpi_total_liabilities = fields.Monetary(readonly=True, currency_field='currency_id')
    kpi_payments_made = fields.Monetary(readonly=True, currency_field='currency_id')
    kpi_payments_received = fields.Monetary(readonly=True, currency_field='currency_id')
    kpi_net_balance = fields.Monetary(readonly=True, currency_field='currency_id')
    kpi_bank_balance = fields.Monetary(readonly=True, currency_field='currency_id')
    kpi_project_available = fields.Monetary(readonly=True, currency_field='currency_id')
    kpi_petty_cash = fields.Monetary(readonly=True, currency_field='currency_id')
    kpi_vendor_liability = fields.Monetary(readonly=True, currency_field='currency_id')
    kpi_sub_liability = fields.Monetary(readonly=True, currency_field='currency_id')

    # ── Bank guarantee KPIs ──────────────────────────────────────────────────
    kpi_bg_total_facility = fields.Monetary(readonly=True, currency_field='currency_id')
    kpi_bg_utilized = fields.Monetary(readonly=True, currency_field='currency_id')
    kpi_bg_available = fields.Monetary(readonly=True, currency_field='currency_id')
    kpi_bg_margin_locked = fields.Monetary(readonly=True, currency_field='currency_id')
    kpi_bg_active_count = fields.Integer(readonly=True)
    kpi_bg_expiring_count = fields.Integer(readonly=True)
    kpi_bg_released_count = fields.Integer(readonly=True)
    kpi_performance_bg = fields.Monetary(readonly=True, currency_field='currency_id')
    kpi_mobilization_bg = fields.Monetary(readonly=True, currency_field='currency_id')

    currency_id = fields.Many2one(
        'res.currency', default=lambda self: self.env.company.currency_id)

    # ── Embedded tables ──────────────────────────────────────────────────────
    project_line_ids = fields.One2many(
        'x.management.dashboard.project.line', 'dashboard_id',
        string='Project Summary', readonly=True)
    vendor_liability_line_ids = fields.One2many(
        'x.management.dashboard.liability.line', 'dashboard_id',
        domain=[('partner_type', '=', 'vendor')],
        string='Vendor Liabilities', readonly=True)
    sub_liability_line_ids = fields.One2many(
        'x.management.dashboard.liability.line', 'dashboard_id',
        domain=[('partner_type', '=', 'subcontractor')],
        string='Subcontractor Liabilities', readonly=True)
    bank_line_ids = fields.One2many(
        'x.management.dashboard.bank.line', 'dashboard_id',
        string='Bank Balances', readonly=True)
    bg_facility_line_ids = fields.One2many(
        'x.management.dashboard.bg.facility.line', 'dashboard_id',
        string='Bank Facilities', readonly=True)
    bg_project_line_ids = fields.One2many(
        'x.management.dashboard.bg.project.line', 'dashboard_id',
        string='Project Bank Guarantees', readonly=True)
    expiring_bg_ids = fields.Many2many(
        'x.bank.guarantee', 'mgmt_dash_expiring_bg_rel', 'dash_id', 'bg_id',
        string='Expiring BGs', readonly=True)

    _FILTER_FIELDS = frozenset({
        'filter_dashboard_tab', 'filter_scope', 'filter_project_id',
        'filter_period_preset', 'filter_date_from', 'filter_date_to',
    })

    # ── Access & defaults ────────────────────────────────────────────────────

    @api.model
    def _can_open_dashboard(self, user=None):
        user = user or self.env.user
        return (
            user._matracon_is_admin()
            or user.has_group('site_operations.group_finance_ho')
            or user.has_group('purchase_demand_raise.group_ceo_approval')
            or user.has_group('site_operations.group_site_accountant')
        )

    @api.model
    def _default_scope_and_project(self):
        user = self.env.user
        analytic = user.x_default_analytic_account_id
        if user.has_group('site_operations.group_site_accountant') and analytic:
            if not user.has_group('site_operations.group_finance_ho') and not user._matracon_is_admin() and not user.has_group('purchase_demand_raise.group_ceo_approval'):
                return 'single', analytic.id
        return 'all', False

    @api.model
    def _default_date_range(self):
        today = fields.Date.context_today(self)
        return today.replace(month=1, day=1), today

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        scope, project_id = self._default_scope_and_project()
        date_from, date_to = self._default_date_range()
        res.setdefault('filter_scope', scope)
        if project_id:
            res.setdefault('filter_project_id', project_id)
        res.setdefault('filter_date_from', date_from)
        res.setdefault('filter_date_to', date_to)
        return res

    # ── Date range helpers ───────────────────────────────────────────────────

    def _resolve_date_range(self):
        self.ensure_one()
        today = fields.Date.context_today(self)
        preset = self.filter_period_preset
        if preset == 'custom':
            return self.filter_date_from, self.filter_date_to
        if preset == 'all':
            return None, None
        if preset == 'monthly':
            return today.replace(day=1), today
        if preset == 'quarterly':
            quarter_start_month = ((today.month - 1) // 3) * 3 + 1
            return today.replace(month=quarter_start_month, day=1), today
        if preset == 'yearly':
            return today.replace(month=1, day=1), today
        return self.filter_date_from, self.filter_date_to

    @api.onchange('filter_period_preset')
    def _onchange_filter_period_preset(self):
        today = fields.Date.context_today(self)
        preset = self.filter_period_preset
        if preset == 'monthly':
            self.filter_date_from = today.replace(day=1)
            self.filter_date_to = today
        elif preset == 'quarterly':
            qm = ((today.month - 1) // 3) * 3 + 1
            self.filter_date_from = today.replace(month=qm, day=1)
            self.filter_date_to = today
        elif preset == 'yearly':
            self.filter_date_from = today.replace(month=1, day=1)
            self.filter_date_to = today
        elif preset == 'all':
            self.filter_date_from = False
            self.filter_date_to = False

    def _active_analytic_accounts(self):
        self.ensure_one()
        site_projects = self.env['x.project.site.config'].search([]).mapped(
            'analytic_account_id')
        if self.filter_scope == 'single' and self.filter_project_id:
            if self.filter_project_id not in site_projects:
                return self.filter_project_id
            return self.filter_project_id
        return site_projects

    def _active_project_records(self):
        analytics = self._active_analytic_accounts()
        if not analytics:
            return self.env['project.project']
        return self.env['project.project'].search([
            ('x_analytic_account_id', 'in', analytics.ids),
        ])

    def _is_subcontractor(self, partner):
        if not partner:
            return False
        return bool(partner.category_id.filtered(
            lambda c: 'subcontractor' in (c.name or '').lower()
        ))

    def _payment_date_domain(self, date_from, date_to):
        if date_from and date_to:
            return [('date', '>=', date_from), ('date', '<=', date_to)]
        if date_from:
            return [('date', '>=', date_from)]
        if date_to:
            return [('date', '<=', date_to)]
        return []

    def _payments_for_analytics(self, analytics, payment_type, date_from, date_to):
        Payment = self.env['account.payment']
        domain = [
            ('payment_type', '=', payment_type),
            ('state', '=', 'posted'),
        ] + self._payment_date_domain(date_from, date_to)
        if analytics:
            domain.append(('x_fund_project_id', 'in', analytics.ids))
        return Payment.search(domain)

    def _period_outbound_for_analytics(self, analytics, date_from, date_to):
        """Outbound spend in period — allocations + direct payments."""
        Allocation = self.env['x.payment.project.allocation']
        Payment = self.env['account.payment']
        total = 0.0
        if not analytics:
            return 0.0
        alloc_domain = [
            ('project_analytic_account_id', 'in', analytics.ids),
            ('payment_id.payment_type', '=', 'outbound'),
            ('payment_id.state', '=', 'posted'),
        ]
        if date_from:
            alloc_domain.append(('payment_id.date', '>=', date_from))
        if date_to:
            alloc_domain.append(('payment_id.date', '<=', date_to))
        total += sum(Allocation.search(alloc_domain).mapped('allocation_amount'))

        direct_domain = [
            ('payment_type', '=', 'outbound'),
            ('state', '=', 'posted'),
            ('x_fund_project_id', 'in', analytics.ids),
            ('x_allocation_ids', '=', False),
        ] + self._payment_date_domain(date_from, date_to)
        total += sum(Payment.search(direct_domain).mapped('amount'))
        return total

    def _bank_balances(self):
        AML = self.env['account.move.line']
        company = self.env.company
        journals = self.env['account.journal'].search([
            ('type', '=', 'bank'),
            ('company_id', '=', company.id),
        ])
        lines = []
        total = 0.0
        for journal in journals:
            account = journal.default_account_id
            if not account:
                continue
            aml = AML.search([
                ('account_id', '=', account.id),
                ('parent_state', '=', 'posted'),
                ('company_id', '=', company.id),
            ])
            balance = sum(aml.mapped('balance'))
            total += balance
            lines.append({
                'journal_id': journal.id,
                'bank_name': journal.name,
                'balance': balance,
                'currency_id': company.currency_id.id,
            })
        return total, lines

    def _liability_lines_for_analytics(self, analytics):
        SheetLine = self.env['x.liability.sheet.line']
        vendor_lines = []
        sub_lines = []
        if not analytics:
            return vendor_lines, sub_lines
        sheet_lines = SheetLine.search([
            ('sheet_id.project_analytic_account_id', 'in', analytics.ids),
            ('sheet_id.state', 'in', ('draft', 'submitted', 'approved')),
        ])
        partner_map = {}
        for line in sheet_lines:
            remaining = max(line.liability_amount - line.paid_amount, 0.0)
            if remaining <= 0:
                continue
            pid = line.partner_id.id
            key = (pid, self._is_subcontractor(line.partner_id))
            if key not in partner_map:
                partner_map[key] = {
                    'partner_id': pid,
                    'partner_name': line.partner_id.display_name,
                    'partner_type': 'subcontractor' if key[1] else 'vendor',
                    'category_label': line.description or (
                        line.partner_id.category_id[:1].name if line.partner_id.category_id else ''
                    ),
                    'total_value': 0.0,
                    'paid_amount': 0.0,
                    'liability_amount': 0.0,
                }
            partner_map[key]['total_value'] += line.liability_amount
            partner_map[key]['paid_amount'] += line.paid_amount
            partner_map[key]['liability_amount'] += remaining

        for data in partner_map.values():
            data['currency_id'] = self.env.company.currency_id.id
            if data['partner_type'] == 'subcontractor':
                sub_lines.append(data)
            else:
                vendor_lines.append(data)
        return vendor_lines, sub_lines

    def _bg_status_for_project(self, project):
        BG = self.env['x.bank.guarantee']
        active = BG.search([
            ('project_id', '=', project.id),
            ('state', 'in', ('pending', 'active', 'locked')),
        ], limit=1)
        if active:
            return active.state if active.state != 'locked' else 'active', active.bg_amount
        expired = BG.search([
            ('project_id', '=', project.id),
            ('state', '=', 'expired'),
        ], limit=1)
        if expired:
            return 'expired', expired.bg_amount
        released = BG.search([
            ('project_id', '=', project.id),
            ('state', '=', 'released'),
        ], limit=1)
        if released:
            return 'released', released.bg_amount
        return 'none', 0.0

    def _compute_kpi_data(self):
        self.ensure_one()
        user = self.env.user
        date_from, date_to = self._resolve_date_range()
        analytics = self._active_analytic_accounts()
        projects = self._active_project_records()
        Payment = self.env['account.payment']
        Allocation = self.env['x.payment.project.allocation']
        AML = self.env['account.move.line']
        currency = self.env.company.currency_id

        funds_received = 0.0
        total_spent_lifetime = 0.0
        available_total = 0.0
        vendor_liability = 0.0
        sub_liability = 0.0
        petty_cash_total = 0.0

        project_line_vals = []
        for project in projects:
            metrics = project._get_fund_metrics(Payment, Allocation, AML)
            funds_received += metrics['x_funds_received']
            total_spent_lifetime += metrics['x_total_spent']
            available_total += metrics['x_available_balance']
            vendor_liability += metrics['x_total_vendor_liability']
            sub_liability += metrics['x_total_sub_liability']

            fund = self.env['x.petty.cash.fund'].search([
                ('project_analytic_account_id', '=', project.x_analytic_account_id.id),
            ], limit=1)
            pc_balance = fund.balance if fund else 0.0
            petty_cash_total += pc_balance

            bg_status, bg_amount = self._bg_status_for_project(project)
            period_paid = self._period_outbound_for_analytics(
                project.x_analytic_account_id, date_from, date_to)

            project_line_vals.append({
                'project_id': project.id,
                'analytic_account_id': project.x_analytic_account_id.id,
                'project_name': project.name,
                'total_value': metrics['x_funds_received'],
                'work_done': metrics['x_total_spent'],
                'funds_received': metrics['x_funds_received'],
                'payments_made': period_paid if (date_from or date_to) else metrics['x_total_spent'],
                'total_liability': (
                    metrics['x_total_vendor_liability'] + metrics['x_total_sub_liability']
                ),
                'available_balance': metrics['x_available_balance'],
                'bg_status': bg_status,
                'bg_amount': bg_amount,
                'petty_cash_balance': pc_balance,
                'currency_id': currency.id,
            })

        total_liabilities = vendor_liability + sub_liability
        payments_received = sum(self._payments_for_analytics(
            analytics, 'inbound', date_from, date_to).mapped('amount'))
        payments_made = self._period_outbound_for_analytics(analytics, date_from, date_to)
        if not date_from and not date_to:
            payments_made = total_spent_lifetime

        bank_total, bank_line_vals = self._bank_balances()
        vendor_lines, sub_lines = self._liability_lines_for_analytics(analytics)

        facilities = self.env['x.bank.guarantee.facility'].search([])
        bg_total_facility = sum(facilities.mapped('total_limit'))
        bg_utilized = sum(facilities.mapped('utilized_amount'))
        bg_available = sum(facilities.mapped('available_limit'))

        bg_domain = [('state', 'in', ('pending', 'active', 'locked'))]
        if self.filter_scope == 'single' and self.filter_project_id:
            project_ids = projects.ids
            bg_domain.append(('project_id', 'in', project_ids))
        active_bgs = self.env['x.bank.guarantee'].search(bg_domain)
        bg_margin = sum(active_bgs.mapped('margin_amount'))
        expiring_domain = [
            ('is_expiring_soon', '=', True),
            ('state', 'in', ('active', 'locked', 'pending')),
        ]
        if self.filter_scope == 'single' and projects:
            expiring_domain.append(('project_id', 'in', projects.ids))
        expiring_bgs = self.env['x.bank.guarantee'].search(expiring_domain)

        released_count = self.env['x.bank.guarantee'].search_count([
            ('state', '=', 'released'),
        ])

        performance_bg = sum(active_bgs.filtered(
            lambda g: g.nature == 'performance').mapped('bg_amount'))
        mobilization_bg = sum(active_bgs.filtered(
            lambda g: g.nature in ('advance_payment', 'bid_bond')).mapped('bg_amount'))

        bg_facility_line_vals = []
        for fac in facilities:
            margin = sum(fac.guarantee_ids.filtered(
                lambda g: g.state in ('pending', 'active', 'locked')
            ).mapped('margin_amount'))
            pct = (fac.utilized_amount / fac.total_limit * 100.0) if fac.total_limit else 0.0
            bg_facility_line_vals.append({
                'facility_id': fac.id,
                'bank_name': fac.bank_id.name or fac.name,
                'total_limit': fac.total_limit,
                'utilized_amount': fac.utilized_amount,
                'margin_amount': margin,
                'available_limit': fac.available_limit,
                'utilization_pct': pct,
                'currency_id': currency.id,
            })

        bg_project_line_vals = []
        project_bgs = self.env['x.bank.guarantee'].search([
            ('project_id', 'in', projects.ids),
            ('state', 'in', ('pending', 'active', 'locked', 'expired')),
        ])
        nature_labels = dict(self.env['x.bank.guarantee']._fields['nature'].selection)
        for bg in project_bgs:
            bg_project_line_vals.append({
                'guarantee_id': bg.id,
                'project_name': bg.project_id.name or '',
                'nature_label': nature_labels.get(bg.nature, bg.nature),
                'guarantee_number': bg.guarantee_number,
                'bg_amount': bg.bg_amount,
                'expiry_date': bg.expiry_date,
                'state': bg.state,
                'currency_id': currency.id,
            })

        tab_titles = {
            'overview': _('Management Dashboard'),
            'project': _('Project Dashboard'),
            'bank_guarantee': _('Bank Guarantee Management'),
        }
        title = tab_titles.get(self.filter_dashboard_tab, _('Management Dashboard'))
        if self.filter_scope == 'single' and self.filter_project_id:
            title = _('%s — %s') % (title, self.filter_project_id.display_name)

        return {
            'name': title,
            'user_name': user.name,
            'available_project_ids': self.env['x.project.site.config'].search([]).mapped(
                'analytic_account_id'),
            'kpi_total_liabilities': total_liabilities,
            'kpi_payments_made': payments_made,
            'kpi_payments_received': payments_received,
            'kpi_net_balance': funds_received - total_spent_lifetime,
            'kpi_bank_balance': bank_total,
            'kpi_project_available': available_total,
            'kpi_petty_cash': petty_cash_total,
            'kpi_vendor_liability': vendor_liability,
            'kpi_sub_liability': sub_liability,
            'kpi_bg_total_facility': bg_total_facility,
            'kpi_bg_utilized': bg_utilized,
            'kpi_bg_available': bg_available,
            'kpi_bg_margin_locked': bg_margin,
            'kpi_bg_active_count': len(active_bgs),
            'kpi_bg_expiring_count': len(expiring_bgs),
            'kpi_bg_released_count': released_count,
            'kpi_performance_bg': performance_bg,
            'kpi_mobilization_bg': mobilization_bg,
            'project_line_ids': project_line_vals,
            'vendor_liability_line_ids': vendor_lines,
            'sub_liability_line_ids': sub_lines,
            'bank_line_ids': bank_line_vals,
            'bg_facility_line_ids': bg_facility_line_vals,
            'bg_project_line_ids': bg_project_line_vals,
            'expiring_bg_ids': expiring_bgs,
        }

    @api.model
    def _refresh_dashboard_data(self, dashboard):
        data = dashboard._compute_kpi_data()
        line_fields = {
            'project_line_ids', 'vendor_liability_line_ids', 'sub_liability_line_ids',
            'bank_line_ids', 'bg_facility_line_ids', 'bg_project_line_ids',
        }
        write_vals = {}
        for fname, val in data.items():
            if fname in line_fields:
                write_vals[fname] = [(5, 0, 0)] + [(0, 0, row) for row in val]
            elif fname == 'expiring_bg_ids':
                write_vals[fname] = [(6, 0, val.ids)]
            elif hasattr(val, '_name'):
                write_vals[fname] = [(6, 0, val.ids)]
            else:
                write_vals[fname] = val
        dashboard.with_context(_refreshing_dashboard=True).write(write_vals)

    def write(self, vals):
        res = super().write(vals)
        if not self.env.context.get('_refreshing_dashboard') and (
            vals.keys() & self._FILTER_FIELDS
        ):
            for rec in self:
                self._refresh_dashboard_data(rec)
        return res

    @api.onchange(
        'filter_dashboard_tab', 'filter_scope', 'filter_project_id',
        'filter_period_preset', 'filter_date_from', 'filter_date_to',
    )
    def _onchange_filters(self):
        if self.filter_scope == 'single' and not self.filter_project_id:
            return
        data = self._compute_kpi_data()
        line_fields = {
            'project_line_ids', 'vendor_liability_line_ids', 'sub_liability_line_ids',
            'bank_line_ids', 'bg_facility_line_ids', 'bg_project_line_ids',
        }
        for fname, val in data.items():
            if fname in line_fields:
                setattr(self, fname, [(5, 0, 0)] + [(0, 0, row) for row in val])
            elif fname == 'expiring_bg_ids':
                setattr(self, fname, val)
            elif hasattr(val, '_name'):
                setattr(self, fname, val)
            else:
                setattr(self, fname, val)

    @api.onchange('filter_scope')
    def _onchange_filter_scope(self):
        if self.filter_scope == 'all':
            self.filter_project_id = False
        elif self.filter_scope == 'single' and not self.filter_project_id:
            user = self.env.user
            if user.x_default_analytic_account_id:
                self.filter_project_id = user.x_default_analytic_account_id

    @api.onchange('filter_dashboard_tab')
    def _onchange_filter_dashboard_tab(self):
        if self.filter_dashboard_tab == 'project' and self.filter_scope == 'all':
            self.filter_scope = 'single'
            if not self.filter_project_id and self.available_project_ids:
                self.filter_project_id = self.available_project_ids[0]

    # ── Window actions ─────────────────────────────────────────────────────────

    @api.model
    def action_open_dashboard(self):
        if not self._can_open_dashboard():
            raise UserError(_(
                'This dashboard is available to CEO, Finance Officer, and Site Accountant only.'
            ))
        dashboard = self.create({})
        self._refresh_dashboard_data(dashboard)
        return {
            'type': 'ir.actions.act_window',
            'name': _('Management Dashboard'),
            'res_model': self._name,
            'res_id': dashboard.id,
            'view_mode': 'form',
            'target': 'current',
        }

    def action_refresh(self):
        self.ensure_one()
        self._refresh_dashboard_data(self)
        return {
            'type': 'ir.actions.act_window',
            'res_model': self._name,
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'current',
        }

    def action_apply_filters(self):
        return self.action_refresh()

    def action_clear_filters(self):
        self.ensure_one()
        scope, project_id = self._default_scope_and_project()
        date_from, date_to = self._default_date_range()
        self.write({
            'filter_dashboard_tab': 'overview',
            'filter_scope': scope,
            'filter_project_id': project_id or False,
            'filter_period_preset': 'yearly',
            'filter_date_from': date_from,
            'filter_date_to': date_to,
        })
        return self.action_refresh()

    def _analytic_domain(self):
        analytics = self._active_analytic_accounts()
        if analytics:
            return [('project_analytic_account_id', 'in', analytics.ids)]
        return []

    def _project_domain(self):
        projects = self._active_project_records()
        if projects:
            return [('project_id', 'in', projects.ids)]
        return []

    def _date_context(self):
        date_from, date_to = self._resolve_date_range()
        ctx = {}
        if date_from:
            ctx['search_default_date_from'] = date_from
        if date_to:
            ctx['search_default_date_to'] = date_to
        return ctx

    def action_open_liability_sheets(self):
        return {
            'type': 'ir.actions.act_window',
            'name': _('Liability Sheets'),
            'res_model': 'x.liability.sheet',
            'view_mode': 'list,form',
            'domain': self._analytic_domain(),
            'context': self._date_context(),
        }

    def action_open_vendor_liabilities(self):
        return {
            'type': 'ir.actions.act_window',
            'name': _('Vendor Liabilities'),
            'res_model': 'x.liability.sheet.line',
            'view_mode': 'list,form',
            'domain': self._analytic_domain() + [
                ('sheet_id.state', 'in', ('draft', 'submitted', 'approved')),
            ],
            'context': self._date_context(),
        }

    def action_open_sub_liabilities(self):
        lines = self.sub_liability_line_ids
        return {
            'type': 'ir.actions.act_window',
            'name': _('Subcontractor Liabilities'),
            'res_model': 'x.liability.sheet.line',
            'view_mode': 'list,form',
            'domain': self._analytic_domain() + [
                ('sheet_id.state', 'in', ('draft', 'submitted', 'approved')),
                ('partner_id', 'in', lines.mapped('partner_id').ids or [0]),
            ],
        }

    def action_open_payments_made(self):
        date_from, date_to = self._resolve_date_range()
        analytics = self._active_analytic_accounts()
        domain = [
            ('payment_type', '=', 'outbound'),
            ('state', '=', 'posted'),
        ] + self._payment_date_domain(date_from, date_to)
        if analytics:
            domain.append(('x_fund_project_id', 'in', analytics.ids))
        return {
            'type': 'ir.actions.act_window',
            'name': _('Payments Made'),
            'res_model': 'account.payment',
            'view_mode': 'list,form',
            'domain': domain,
        }

    def action_open_payments_received(self):
        date_from, date_to = self._resolve_date_range()
        analytics = self._active_analytic_accounts()
        domain = [
            ('payment_type', '=', 'inbound'),
            ('state', '=', 'posted'),
        ] + self._payment_date_domain(date_from, date_to)
        if analytics:
            domain.append(('x_fund_project_id', 'in', analytics.ids))
        return {
            'type': 'ir.actions.act_window',
            'name': _('Payments Received'),
            'res_model': 'account.payment',
            'view_mode': 'list,form',
            'domain': domain,
        }

    def action_open_project_financial_overview(self):
        projects = self._active_project_records()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Project Financial Overview'),
            'res_model': 'project.project',
            'view_mode': 'list,form',
            'domain': [('id', 'in', projects.ids)],
            'context': {'search_default_group_by': False},
        }

    def action_open_petty_cash(self):
        analytics = self._active_analytic_accounts()
        domain = []
        if analytics:
            domain = [('project_analytic_account_id', 'in', analytics.ids)]
        return {
            'type': 'ir.actions.act_window',
            'name': _('Petty Cash Funds'),
            'res_model': 'x.petty.cash.fund',
            'view_mode': 'list,form',
            'domain': domain,
        }

    def action_open_petty_cash_expenses(self):
        analytics = self._active_analytic_accounts()
        date_from, date_to = self._resolve_date_range()
        domain = [('state', '=', 'posted')]
        if analytics:
            domain.append(('fund_id.project_analytic_account_id', 'in', analytics.ids))
        if date_from:
            domain.append(('expense_date', '>=', date_from))
        if date_to:
            domain.append(('expense_date', '<=', date_to))
        return {
            'type': 'ir.actions.act_window',
            'name': _('Petty Cash Expenses'),
            'res_model': 'x.petty.cash.expense',
            'view_mode': 'list,form',
            'domain': domain,
        }

    def action_open_bank_guarantees(self):
        domain = []
        proj_domain = self._project_domain()
        if proj_domain:
            domain = proj_domain
        return {
            'type': 'ir.actions.act_window',
            'name': _('Bank Guarantees'),
            'res_model': 'x.bank.guarantee',
            'view_mode': 'list,form',
            'domain': domain,
        }

    def action_open_expiring_bgs(self):
        return {
            'type': 'ir.actions.act_window',
            'name': _('Expiring Bank Guarantees'),
            'res_model': 'x.bank.guarantee',
            'view_mode': 'list,form',
            'domain': [('id', 'in', self.expiring_bg_ids.ids)],
        }

    def action_open_bg_facilities(self):
        return {
            'type': 'ir.actions.act_window',
            'name': _('Bank Facility Limits'),
            'res_model': 'x.bank.guarantee.facility',
            'view_mode': 'list,form',
        }

    def action_open_bank_journals(self):
        return {
            'type': 'ir.actions.act_window',
            'name': _('Bank Journals'),
            'res_model': 'account.journal',
            'view_mode': 'list,form',
            'domain': [('type', '=', 'bank')],
        }

    def action_open_tax_notices(self):
        return {
            'type': 'ir.actions.act_window',
            'name': _('Tax Notices & Orders'),
            'res_model': 'x.tax.notice.order',
            'view_mode': 'list,form',
        }

    def action_open_project_line(self):
        """Open selected project from summary table."""
        self.ensure_one()
        return self.action_open_project_financial_overview()
