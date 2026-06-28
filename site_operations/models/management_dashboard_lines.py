"""Management dashboard line models — embedded tables for x.management.dashboard."""

from odoo import models, fields


class ManagementDashboardProjectLine(models.TransientModel):
    _name = 'x.management.dashboard.project.line'
    _description = 'Management Dashboard Project Summary Line'
    _order = 'project_name'

    dashboard_id = fields.Many2one(
        'x.management.dashboard', ondelete='cascade', required=True)
    project_id = fields.Many2one('project.project', readonly=True)
    analytic_account_id = fields.Many2one('account.analytic.account', readonly=True)
    project_name = fields.Char(readonly=True)
    total_value = fields.Monetary(readonly=True, currency_field='currency_id')
    work_done = fields.Monetary(
        string='Work Done (Paid Out)', readonly=True, currency_field='currency_id')
    funds_received = fields.Monetary(readonly=True, currency_field='currency_id')
    payments_made = fields.Monetary(readonly=True, currency_field='currency_id')
    total_liability = fields.Monetary(readonly=True, currency_field='currency_id')
    available_balance = fields.Monetary(readonly=True, currency_field='currency_id')
    bg_status = fields.Selection([
        ('active', 'Active'),
        ('expired', 'Expired'),
        ('pending', 'Pending'),
        ('released', 'Released'),
        ('none', 'None'),
    ], readonly=True)
    bg_amount = fields.Monetary(readonly=True, currency_field='currency_id')
    petty_cash_balance = fields.Monetary(readonly=True, currency_field='currency_id')
    contract_value = fields.Monetary(readonly=True, currency_field='currency_id')
    billed_to_client = fields.Monetary(readonly=True, currency_field='currency_id')
    work_completion_pct = fields.Float(string='Work %', digits=(5, 1), readonly=True)
    financial_completion_pct = fields.Float(string='Financial %', digits=(5, 1), readonly=True)
    remaining_work_value = fields.Monetary(readonly=True, currency_field='currency_id')
    currency_id = fields.Many2one('res.currency', readonly=True)


class ManagementDashboardLiabilityLine(models.TransientModel):
    _name = 'x.management.dashboard.liability.line'
    _description = 'Management Dashboard Liability Line'
    _order = 'partner_name'

    dashboard_id = fields.Many2one(
        'x.management.dashboard', ondelete='cascade', required=True)
    partner_id = fields.Many2one('res.partner', readonly=True)
    partner_name = fields.Char(readonly=True)
    partner_type = fields.Selection([
        ('vendor', 'Vendor'),
        ('subcontractor', 'Subcontractor'),
    ], readonly=True)
    category_label = fields.Char(
        string='Type / Material', readonly=True,
        help='Work type for subcontractors or material type for vendors.')
    total_value = fields.Monetary(readonly=True, currency_field='currency_id')
    paid_amount = fields.Monetary(readonly=True, currency_field='currency_id')
    liability_amount = fields.Monetary(readonly=True, currency_field='currency_id')
    currency_id = fields.Many2one('res.currency', readonly=True)


class ManagementDashboardBankLine(models.TransientModel):
    _name = 'x.management.dashboard.bank.line'
    _description = 'Management Dashboard Bank Balance Line'
    _order = 'bank_name'

    dashboard_id = fields.Many2one(
        'x.management.dashboard', ondelete='cascade', required=True)
    journal_id = fields.Many2one('account.journal', readonly=True)
    bank_name = fields.Char(readonly=True)
    balance = fields.Monetary(readonly=True, currency_field='currency_id')
    currency_id = fields.Many2one('res.currency', readonly=True)


class ManagementDashboardBgFacilityLine(models.TransientModel):
    _name = 'x.management.dashboard.bg.facility.line'
    _description = 'Management Dashboard BG Facility Line'
    _order = 'bank_name'

    dashboard_id = fields.Many2one(
        'x.management.dashboard', ondelete='cascade', required=True)
    facility_id = fields.Many2one('x.bank.guarantee.facility', readonly=True)
    bank_name = fields.Char(readonly=True)
    total_limit = fields.Monetary(readonly=True, currency_field='currency_id')
    utilized_amount = fields.Monetary(readonly=True, currency_field='currency_id')
    margin_amount = fields.Monetary(readonly=True, currency_field='currency_id')
    available_limit = fields.Monetary(readonly=True, currency_field='currency_id')
    utilization_pct = fields.Float(readonly=True, digits=(5, 1))
    currency_id = fields.Many2one('res.currency', readonly=True)


class ManagementDashboardBgProjectLine(models.TransientModel):
    _name = 'x.management.dashboard.bg.project.line'
    _description = 'Management Dashboard Project BG Line'
    _order = 'project_name, nature_label'

    dashboard_id = fields.Many2one(
        'x.management.dashboard', ondelete='cascade', required=True)
    guarantee_id = fields.Many2one('x.bank.guarantee', readonly=True)
    project_name = fields.Char(readonly=True)
    nature_label = fields.Char(readonly=True)
    guarantee_number = fields.Char(readonly=True)
    bg_amount = fields.Monetary(readonly=True, currency_field='currency_id')
    expiry_date = fields.Date(readonly=True)
    state = fields.Char(readonly=True)
    currency_id = fields.Many2one('res.currency', readonly=True)


class ManagementDashboardAttendanceLine(models.TransientModel):
    _name = 'x.management.dashboard.attendance.line'
    _description = 'Management Dashboard Attendance Line'
    _order = 'project_name'

    dashboard_id = fields.Many2one(
        'x.management.dashboard', ondelete='cascade', required=True)
    project_name = fields.Char(readonly=True)
    employee_count = fields.Integer(string='Total Employees', readonly=True)
    monthly_present_days = fields.Integer(string='Present (Man-Days)', readonly=True)
    attendance_pct = fields.Float(string='Attendance %', readonly=True, digits=(5, 1))
    petty_cash_balance = fields.Monetary(
        string='Avail. Petty Cash', readonly=True, currency_field='currency_id')
    currency_id = fields.Many2one('res.currency', readonly=True)


class ManagementDashboardIpcLine(models.TransientModel):
    _name = 'x.management.dashboard.ipc.line'
    _description = 'Management Dashboard IPC Line'
    _order = 'ipc_date desc'

    dashboard_id = fields.Many2one('x.management.dashboard', ondelete='cascade', required=True)
    ipc_name = fields.Char(string='IPC No', readonly=True)
    subcontractor_name = fields.Char(string='Subcontractor', readonly=True)
    project_name = fields.Char(string='Project', readonly=True)
    period = fields.Char(readonly=True)
    ipc_date = fields.Date(string='IPC Date', readonly=True)
    gross_work_done = fields.Monetary(readonly=True, currency_field='currency_id')
    total_deductions = fields.Monetary(readonly=True, currency_field='currency_id')
    net_payable = fields.Monetary(readonly=True, currency_field='currency_id')
    state = fields.Char(readonly=True)
    currency_id = fields.Many2one('res.currency', readonly=True)
