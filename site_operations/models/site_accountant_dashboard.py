"""Site Accountant Dashboard — project-scoped overview for site accountants."""

import calendar
import datetime as dt
from dateutil.relativedelta import relativedelta

from odoo import models, fields, api, _
from odoo.exceptions import UserError


class SiteAccountantDashboard(models.TransientModel):
    _name = 'x.site.accountant.dashboard'
    _description = 'Site Accountant Dashboard'

    name = fields.Char(readonly=True)          # project name
    user_name = fields.Char(readonly=True)
    project_analytic_id = fields.Many2one('account.analytic.account', readonly=True)

    # ── Attendance KPIs ──────────────────────────────────────────────────────
    kpi_total_employees = fields.Integer(string='Total Employees', readonly=True)
    kpi_monthly_present_days = fields.Integer(string='Monthly Present (Man-Days)', readonly=True)
    kpi_capacity_utilization_pct = fields.Float(
        string='Capacity Utilization %', readonly=True, digits=(5, 1))
    kpi_6m_avg_present = fields.Float(
        string='6-Month Avg Present (Man-Days)', readonly=True, digits=(16, 1))
    kpi_monthly_attendance_pct = fields.Float(
        string='Monthly Attendance %', readonly=True, digits=(5, 1))
    kpi_6m_avg_staff = fields.Float(
        string='6-Month Avg Staff', readonly=True, digits=(16, 1))

    # ── Financial KPIs ────────────────────────────────────────────────────────
    kpi_petty_cash_balance = fields.Monetary(
        string='Available Petty Cash', readonly=True, currency_field='currency_id')
    kpi_sub_liability = fields.Monetary(
        string='Total Sub Liability', readonly=True, currency_field='currency_id')
    kpi_vendor_liability = fields.Monetary(
        string='Total Vendor Liability', readonly=True, currency_field='currency_id')
    currency_id = fields.Many2one(
        'res.currency', default=lambda self: self.env.company.currency_id)

    # ── Embedded tables ───────────────────────────────────────────────────────
    sub_liability_line_ids = fields.One2many(
        'x.site.dashboard.liability.line', 'dashboard_id',
        domain=[('partner_type', '=', 'subcontractor')],
        string='Subcontractor Liabilities', readonly=True)
    vendor_liability_line_ids = fields.One2many(
        'x.site.dashboard.liability.line', 'dashboard_id',
        domain=[('partner_type', '=', 'vendor')],
        string='Vendor Liabilities', readonly=True)
    replenishment_line_ids = fields.One2many(
        'x.site.dashboard.replenishment.line', 'dashboard_id',
        string='Recent Replenishments', readonly=True)
    expense_line_ids = fields.One2many(
        'x.site.dashboard.expense.line', 'dashboard_id',
        string='Daily Ledger', readonly=True)

    # ── Access ────────────────────────────────────────────────────────────────

    @api.model
    def _can_open(self, user=None):
        user = user or self.env.user
        return (
            user._matracon_is_admin()
            or user.has_group('site_operations.group_site_accountant')
            or user.has_group('site_operations.group_finance_ho')
            or user.has_group('purchase_demand_raise.group_ceo_approval')
        )

    def _is_subcontractor(self, partner):
        if not partner:
            return False
        return bool(partner.category_id.filtered(
            lambda c: 'subcontractor' in (c.name or '').lower()
        ))

    # ── Data computation ──────────────────────────────────────────────────────

    def _compute_data(self, analytic):
        """Compute all dashboard data for the given analytic account."""
        self.ensure_one()
        currency = self.env.company.currency_id
        today = fields.Date.context_today(self)
        month_start = today.replace(day=1)
        six_months_ago = month_start - relativedelta(months=6)

        # ── Attendance ────────────────────────────────────────────────────────
        AttSheet = self.env['x.attendance.sheet'].sudo()
        six_month_sheets = AttSheet.search([
            ('project_analytic_account_id', '=', analytic.id),
            ('state', 'in', ('verified', 'posted')),
            ('date_from', '>=', six_months_ago),
        ])
        current_sheets = six_month_sheets.filtered(
            lambda s: s.date_from >= month_start
            or (s.date_from < month_start and s.date_to >= month_start)
        )
        if not current_sheets:
            current_sheets = AttSheet.search([
                ('project_analytic_account_id', '=', analytic.id),
                ('state', 'in', ('verified', 'posted')),
            ], order='date_to desc', limit=1)

        total_employees = sum(current_sheets.mapped('employee_count'))
        monthly_present = sum(current_sheets.mapped('total_present_days'))
        monthly_att_pct = (
            sum(s.avg_present_pct for s in current_sheets) / len(current_sheets)
            if current_sheets else 0.0
        )
        monthly_groups = {}
        for s in six_month_sheets:
            key = (s.date_from.year, s.date_from.month)
            if key not in monthly_groups:
                monthly_groups[key] = {'employees': 0, 'present': 0}
            monthly_groups[key]['employees'] += s.employee_count
            monthly_groups[key]['present'] += s.total_present_days
        months_count = len(monthly_groups)
        avg_present = (
            sum(g['present'] for g in monthly_groups.values()) / months_count
            if months_count else float(monthly_present)
        )
        avg_staff = (
            sum(g['employees'] for g in monthly_groups.values()) / months_count
            if months_count else float(total_employees)
        )
        working_days = sum(
            1 for d in range(1, calendar.monthrange(today.year, today.month)[1] + 1)
            if dt.date(today.year, today.month, d).weekday() < 5
        )
        capacity_pct = (
            (monthly_present / (total_employees * working_days) * 100.0)
            if total_employees and working_days else 0.0
        )

        # ── Petty Cash ────────────────────────────────────────────────────────
        fund = self.env['x.petty.cash.fund'].search([
            ('project_analytic_account_id', '=', analytic.id),
        ], limit=1)
        pc_balance = fund.balance if fund else 0.0

        # Replenishments (last 5 released requests)
        replenishments = []
        if fund:
            requests = self.env['x.petty.cash.request'].search([
                ('fund_id', '=', fund.id),
                ('state', 'in', ('released', 'confirmed')),
            ], order='write_date desc', limit=5)
            for req in requests:
                if req.payment_id:
                    desc = '%s (%s)' % (
                        req.payment_id.name or req.name,
                        req.payment_id.journal_id.name or 'Bank',
                    )
                else:
                    desc = req.name or _('Replenishment')
                replenishments.append({
                    'description': desc,
                    'amount': req.released_amount or 0.0,
                    'currency_id': currency.id,
                })

        # Expenses (last 5 posted)
        expenses = []
        if fund:
            posted_expenses = self.env['x.petty.cash.expense'].search([
                ('fund_id', '=', fund.id),
                ('state', '=', 'posted'),
            ], order='expense_date desc', limit=5)
            for exp in posted_expenses:
                expenses.append({
                    'description': exp.name,
                    'amount': exp.amount,
                    'expense_date': exp.expense_date,
                    'category': exp.category or 'other',
                    'currency_id': currency.id,
                })

        # ── Liabilities ───────────────────────────────────────────────────────
        SheetLine = self.env['x.liability.sheet.line']
        sheet_lines = SheetLine.search([
            ('sheet_id.project_analytic_account_id', '=', analytic.id),
            ('sheet_id.state', 'in', ('draft', 'submitted', 'approved')),
        ])
        partner_map = {}
        for line in sheet_lines:
            remaining = max(line.liability_amount - line.paid_amount, 0.0)
            if remaining <= 0:
                continue
            pid = line.partner_id.id
            is_sub = self._is_subcontractor(line.partner_id)
            key = (pid, is_sub)
            if key not in partner_map:
                partner_map[key] = {
                    'partner_name': line.partner_id.display_name,
                    'partner_type': 'subcontractor' if is_sub else 'vendor',
                    'category_label': line.description or (
                        line.partner_id.category_id[:1].name
                        if line.partner_id.category_id else ''
                    ),
                    'total_value': 0.0,
                    'paid_amount': 0.0,
                    'liability_amount': 0.0,
                }
            partner_map[key]['total_value'] += line.liability_amount
            partner_map[key]['paid_amount'] += line.paid_amount
            partner_map[key]['liability_amount'] += remaining

        liability_lines = []
        sub_total = 0.0
        vendor_total = 0.0
        for data in partner_map.values():
            data['currency_id'] = currency.id
            liability_lines.append(data)
            if data['partner_type'] == 'subcontractor':
                sub_total += data['liability_amount']
            else:
                vendor_total += data['liability_amount']

        return {
            'kpi_total_employees': total_employees,
            'kpi_monthly_present_days': monthly_present,
            'kpi_capacity_utilization_pct': capacity_pct,
            'kpi_6m_avg_present': avg_present,
            'kpi_monthly_attendance_pct': monthly_att_pct,
            'kpi_6m_avg_staff': avg_staff,
            'kpi_petty_cash_balance': pc_balance,
            'kpi_sub_liability': sub_total,
            'kpi_vendor_liability': vendor_total,
            'sub_liability_line_ids': liability_lines,
            'vendor_liability_line_ids': liability_lines,
            'replenishment_line_ids': replenishments,
            'expense_line_ids': expenses,
        }

    def action_refresh(self):
        """Recompute dashboard data and reload."""
        self.ensure_one()
        analytic = self.project_analytic_id
        if not analytic:
            return
        data = self._compute_data(analytic)
        line_fields = {
            'sub_liability_line_ids', 'vendor_liability_line_ids',
            'replenishment_line_ids', 'expense_line_ids',
        }
        write_vals = {}
        for fname, val in data.items():
            if fname in line_fields:
                write_vals[fname] = [(5, 0, 0)] + [(0, 0, row) for row in val]
            else:
                write_vals[fname] = val
        self.write(write_vals)
        return {
            'type': 'ir.actions.act_window',
            'res_model': self._name,
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'main',
            'flags': {'mode': 'readonly'},
        }

    def action_view_petty_cash(self):
        return {
            'type': 'ir.actions.act_window',
            'name': _('Petty Cash'),
            'res_model': 'x.petty.cash.fund',
            'view_mode': 'list,form',
            'domain': [('project_analytic_account_id', '=', self.project_analytic_id.id)],
        }

    @api.model
    def action_open_site_dashboard(self):
        if not self._can_open():
            raise UserError(_(
                'This dashboard is available to Site Accountants, Finance Officers, and Admins.'
            ))
        user = self.env.user
        # Determine the analytic account to show
        analytic = user.x_default_analytic_account_id
        # FO/CEO/Admin with no default: pick first site project
        if not analytic:
            config = self.env['x.project.site.config'].search([], limit=1)
            analytic = config.analytic_account_id if config else False
        if not analytic:
            raise UserError(_('No project is configured for your account.'))

        project_name = analytic.name
        dashboard = self.create({
            'name': project_name,
            'user_name': user.name,
            'project_analytic_id': analytic.id,
        })
        data = dashboard._compute_data(analytic)
        line_fields = {
            'sub_liability_line_ids', 'vendor_liability_line_ids',
            'replenishment_line_ids', 'expense_line_ids',
        }
        write_vals = {}
        for fname, val in data.items():
            if fname in line_fields:
                write_vals[fname] = [(5, 0, 0)] + [(0, 0, row) for row in val]
            else:
                write_vals[fname] = val
        dashboard.write(write_vals)
        return {
            'type': 'ir.actions.act_window',
            'name': _('Site Overview'),
            'res_model': self._name,
            'res_id': dashboard.id,
            'view_mode': 'form',
            'target': 'main',
            'flags': {'mode': 'readonly'},
        }


class SiteDashboardLiabilityLine(models.TransientModel):
    _name = 'x.site.dashboard.liability.line'
    _description = 'Site Dashboard Liability Line'
    _order = 'partner_name'

    dashboard_id = fields.Many2one(
        'x.site.accountant.dashboard', ondelete='cascade', required=True)
    partner_name = fields.Char(readonly=True)
    partner_type = fields.Selection([
        ('vendor', 'Vendor'),
        ('subcontractor', 'Subcontractor'),
    ], readonly=True)
    category_label = fields.Char(readonly=True)
    total_value = fields.Monetary(string='Work Certified', readonly=True, currency_field='currency_id')
    paid_amount = fields.Monetary(readonly=True, currency_field='currency_id')
    liability_amount = fields.Monetary(string='Net Payable', readonly=True, currency_field='currency_id')
    currency_id = fields.Many2one('res.currency', readonly=True)


class SiteDashboardReplenishmentLine(models.TransientModel):
    _name = 'x.site.dashboard.replenishment.line'
    _description = 'Site Dashboard Petty Cash Replenishment Line'

    dashboard_id = fields.Many2one(
        'x.site.accountant.dashboard', ondelete='cascade', required=True)
    description = fields.Char(readonly=True)
    amount = fields.Monetary(readonly=True, currency_field='currency_id')
    currency_id = fields.Many2one('res.currency', readonly=True)


class SiteDashboardExpenseLine(models.TransientModel):
    _name = 'x.site.dashboard.expense.line'
    _description = 'Site Dashboard Petty Cash Expense Line'

    dashboard_id = fields.Many2one(
        'x.site.accountant.dashboard', ondelete='cascade', required=True)
    description = fields.Char(readonly=True)
    amount = fields.Monetary(readonly=True, currency_field='currency_id')
    expense_date = fields.Date(readonly=True)
    category = fields.Char(readonly=True)
    currency_id = fields.Many2one('res.currency', readonly=True)
