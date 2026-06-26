import calendar
from markupsafe import Markup

from odoo import models, fields, api, _
from odoo.exceptions import UserError

from . import matracon_notifications as matracon_notify


class SalarySheet(models.Model):
    _name = 'x.salary.sheet'
    _description = 'Project Salary Sheet'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'date_from desc, id desc'

    name = fields.Char(compute='_compute_name', store=True, readonly=True)
    project_analytic_account_id = fields.Many2one(
        'account.analytic.account', string='Site Project',
        required=True, tracking=True, index=True)
    attendance_sheet_id = fields.Many2one(
        'x.attendance.sheet', string='Attendance Sheet', readonly=True)
    date_from = fields.Date(required=True, tracking=True)
    date_to = fields.Date(required=True, tracking=True)
    state = fields.Selection([
        ('draft', 'Draft'),
        ('submitted', 'Submitted'),
        ('approved', 'Approved'),
        ('paid', 'Paid'),
    ], default='draft', tracking=True, required=True)

    line_ids = fields.One2many('x.salary.sheet.line', 'sheet_id', string='Employees')
    payment_ids = fields.One2many(
        'account.payment', 'x_salary_sheet_id', string='Payments', readonly=True)

    workforce_count = fields.Integer(compute='_compute_totals', store=True)
    total_basic = fields.Monetary(compute='_compute_totals', store=True)
    total_allowances = fields.Monetary(compute='_compute_totals', store=True)
    total_deductions = fields.Monetary(compute='_compute_totals', store=True)
    total_net = fields.Monetary(compute='_compute_totals', store=True)
    currency_id = fields.Many2one(
        'res.currency', default=lambda self: self.env.company.currency_id)

    @api.depends('project_analytic_account_id', 'date_from', 'date_to')
    def _compute_name(self):
        for sheet in self:
            proj = (
                sheet.project_analytic_account_id.code
                or sheet.project_analytic_account_id.name or ''
            )
            month = sheet.date_from.strftime('%b %Y') if sheet.date_from else ''
            sheet.name = f'Salary/{proj}/{month}' if proj else f'Salary/{month}'

    @api.depends(
        'line_ids.basic_salary', 'line_ids.allowances',
        'line_ids.deductions', 'line_ids.net_payable',
    )
    def _compute_totals(self):
        for sheet in self:
            sheet.workforce_count = len(sheet.line_ids)
            sheet.total_basic = sum(sheet.line_ids.mapped('basic_salary'))
            sheet.total_allowances = sum(sheet.line_ids.mapped('allowances'))
            sheet.total_deductions = sum(sheet.line_ids.mapped('deductions'))
            sheet.total_net = sum(sheet.line_ids.mapped('net_payable'))

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        for sheet in records:
            sheet._generate_lines_from_attendance()
        return records

    def _generate_lines_from_attendance(self):
        for sheet in self:
            if not sheet.attendance_sheet_id:
                continue
            if sheet.line_ids:
                continue
            days_in_month = calendar.monthrange(
                sheet.date_from.year, sheet.date_from.month)[1] if sheet.date_from else 30
            for att_line in sheet.attendance_sheet_id.line_ids:
                emp = att_line.employee_id
                paid_days = att_line.paid_days
                factor = paid_days / days_in_month if days_in_month else 0.0
                basic = (emp.x_basic_salary or 0.0) * factor
                allowances = ((emp.x_hra or 0.0) + (emp.x_site_allowance or 0.0)) * factor
                gross = basic + allowances
                wht = gross * (emp.x_wht_rate or 0.0) / 100.0
                eobi = emp.x_eobi_amount or 0.0
                advance = min(emp.x_advance_balance or 0.0, gross)
                deductions = wht + eobi + advance
                net = max(gross - deductions, 0.0)
                self.env['x.salary.sheet.line'].create({
                    'sheet_id': sheet.id,
                    'employee_id': emp.id,
                    'attendance_line_id': att_line.id,
                    'paid_days': paid_days,
                    'basic_salary': round(basic, 2),
                    'allowances': round(allowances, 2),
                    'deductions': round(deductions, 2),
                    'net_payable': round(net, 2),
                    'detail_hra': round((emp.x_hra or 0.0) * factor, 2),
                    'detail_site_allowance': round((emp.x_site_allowance or 0.0) * factor, 2),
                    'detail_wht': round(wht, 2),
                    'detail_eobi': round(eobi, 2),
                    'detail_advance': round(advance, 2),
                })

    def action_refresh_from_attendance(self):
        for sheet in self:
            if sheet.state != 'draft':
                raise UserError(_('Only draft salary sheets can be refreshed.'))
            sheet.line_ids.unlink()
            sheet._generate_lines_from_attendance()

    def action_submit(self):
        for sheet in self:
            if not sheet.line_ids:
                raise UserError(_('Generate salary lines before submitting.'))
            sheet.state = 'submitted'
            sheet.message_post(
                body=Markup(_('Salary sheet submitted by <b>%s</b>.')) % self.env.user.name)
            fo_users = self.env['res.users'].search([
                ('group_ids', 'in', self.env.ref(
                    'site_operations.group_finance_ho').id),
            ])
            matracon_notify.notify_users(
                sheet, fo_users,
                _('Salary sheet <b>%s</b> submitted for Finance HO.') % sheet.name,
                summary=_('Salary Sheet Submitted'),
            )

    def action_create_payment(self):
        """Finance HO creates consolidated salary payment (CEO approval if FO)."""
        self.ensure_one()
        if self.state not in ('submitted', 'approved'):
            raise UserError(_('Submit the salary sheet before creating payment.'))
        Payment = self.env['account.payment']
        payment = Payment.create({
            'payment_type': 'outbound',
            'partner_type': 'supplier',
            'amount': self.total_net,
            'x_gross_approved_amount': self.total_net,
            'x_salary_sheet_id': self.id,
            'x_payment_category': 'salary',
            'x_destination_project_id': self.project_analytic_account_id.id,
        })
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'account.payment',
            'view_mode': 'form',
            'res_id': payment.id,
        }

    def action_view_payments(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Salary Payments'),
            'res_model': 'account.payment',
            'view_mode': 'list,form',
            'domain': [('x_salary_sheet_id', '=', self.id)],
        }


class SalarySheetLine(models.Model):
    _name = 'x.salary.sheet.line'
    _description = 'Salary Sheet Line'
    _order = 'employee_id'

    sheet_id = fields.Many2one(
        'x.salary.sheet', required=True, ondelete='cascade', index=True)
    employee_id = fields.Many2one('hr.employee', required=True, string='Employee')
    attendance_line_id = fields.Many2one('x.attendance.line', readonly=True)
    paid_days = fields.Integer(string='Paid Days', readonly=True)

    basic_salary = fields.Monetary(currency_field='currency_id')
    allowances = fields.Monetary(currency_field='currency_id')
    deductions = fields.Monetary(currency_field='currency_id')
    net_payable = fields.Monetary(currency_field='currency_id')

    detail_hra = fields.Monetary(string='HRA', currency_field='currency_id')
    detail_site_allowance = fields.Monetary(
        string='Site Allowance', currency_field='currency_id')
    detail_wht = fields.Monetary(string='WHT', currency_field='currency_id')
    detail_eobi = fields.Monetary(string='EOBI', currency_field='currency_id')
    detail_advance = fields.Monetary(string='Advance Recovery', currency_field='currency_id')

    currency_id = fields.Many2one(
        related='sheet_id.currency_id', depends=['sheet_id'])

    def action_open_slip(self):
        self.ensure_one()
        return self.env.ref(
            'site_operations.action_report_salary_slip').report_action(self)
