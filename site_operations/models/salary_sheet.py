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
    total_wht = fields.Monetary(compute='_compute_totals', store=False)
    currency_id = fields.Many2one(
        'res.currency', default=lambda self: self.env.company.currency_id)
    wht_certificate_count = fields.Integer(compute='_compute_wht_certificate_count')

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
            sheet.total_wht = sum(sheet.line_ids.mapped('detail_wht'))

    def _compute_wht_certificate_count(self):
        WHTCert = self.env['x.wht.certificate']
        for sheet in self:
            sheet.wht_certificate_count = WHTCert.search_count([
                ('x_salary_sheet_line_id.sheet_id', '=', sheet.id)
            ])

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
                emp = att_line.employee_id.sudo()
                paid_days = att_line.paid_days
                factor = paid_days / days_in_month if days_in_month else 0.0
                basic = (emp.x_basic_salary or 0.0) * factor
                allowances = ((emp.x_hra or 0.0) + (emp.x_site_allowance or 0.0)) * factor
                gross = basic + allowances
                wht = gross * (emp.x_wht_rate or 0.0) / 100.0
                eobi = emp.x_eobi_amount or 0.0
                # Use per-line advance if set (from CSV import), else employee balance
                if att_line.x_advance_amount:
                    advance = min(att_line.x_advance_amount, gross)
                else:
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
            sheet.line_ids.sudo().unlink()
            sheet._generate_lines_from_attendance()

    def action_submit(self):
        for sheet in self:
            if not sheet.line_ids:
                raise UserError(_('Generate salary lines before submitting.'))
            sheet.state = 'submitted'
            sheet.message_post(
                body=Markup(_('Salary sheet submitted by <b>%s</b>.')) % self.env.user.name)
            # Notify CEO for approval
            ceo_users = self.env['res.users'].search([
                ('group_ids', 'in', self.env.ref(
                    'purchase_demand_raise.group_ceo_approval').id),
            ])
            matracon_notify.notify_users(
                sheet, ceo_users,
                _('Salary sheet <b>%s</b> requires your approval.') % sheet.name,
                summary=_('Salary Sheet Approval Required'),
            )
            matracon_notify.schedule_activity(
                sheet, ceo_users,
                _('Approve salary sheet %s') % sheet.name)

    def action_ceo_approve(self):
        """CEO approves the salary sheet — unlocks payment creation for FO."""
        for sheet in self:
            if sheet.state != 'submitted':
                raise UserError(_('Only submitted salary sheets can be approved.'))
            sheet.state = 'approved'
            sheet.message_post(
                body=Markup(
                    _('Salary sheet approved by CEO <b>%s</b>.')
                ) % self.env.user.name)
            fo_users = self.env['res.users'].search([
                ('group_ids', 'in', self.env.ref(
                    'site_operations.group_finance_ho').id),
            ])
            matracon_notify.notify_users(
                sheet, fo_users,
                _('CEO approved salary sheet <b>%s</b> — create and post payment.') % sheet.name,
                summary=_('Salary Sheet Approved by CEO'),
            )
            matracon_notify.schedule_activity(
                sheet, fo_users,
                _('Create salary payment for %s') % sheet.name)

    def action_create_payment(self):
        """Finance HO creates consolidated salary payment after CEO approval."""
        self.ensure_one()
        if self.state != 'approved':
            raise UserError(_(
                'CEO must approve the salary sheet before a payment can be created.'))
        analytic = self.project_analytic_account_id
        Payment = self.env['account.payment']

        # Gross = basic + all allowances; deductions shown in Tax Compliance tab
        gross = self.total_basic + self.total_allowances

        # Build tax compliance lines for the payment from salary sheet totals
        total_wht = sum(self.line_ids.mapped('detail_wht'))
        total_eobi = sum(self.line_ids.mapped('detail_eobi'))
        total_advance = sum(self.line_ids.mapped('detail_advance'))
        tax_lines = []
        if total_wht:
            tax_lines.append((0, 0, {
                'name': _('Salary Tax WHT (Sec 149)'),
                'tax_type': 'wht',
                'effect': 'deduct',
                'sequence': 10,
                'x_fixed_amount': round(total_wht, 2),
            }))
        if total_eobi:
            tax_lines.append((0, 0, {
                'name': _('EOBI Contribution'),
                'tax_type': 'other',
                'effect': 'deduct',
                'sequence': 20,
                'x_fixed_amount': round(total_eobi, 2),
            }))
        if total_advance:
            tax_lines.append((0, 0, {
                'name': _('Advance Recovery'),
                'tax_type': 'other',
                'effect': 'deduct',
                'sequence': 30,
                'x_fixed_amount': round(total_advance, 2),
            }))

        payment = Payment.create({
            'payment_type': 'outbound',
            'partner_type': 'supplier',
            'amount': self.total_net,
            'x_gross_approved_amount': round(gross, 2) if gross else self.total_net,
            'x_salary_sheet_id': self.id,
            'x_payment_category': 'salary',
            'x_destination_project_id': analytic.id if analytic else False,
            # CEO already approved at sheet level — skip payment-level approval
            'x_ceo_approval_state': 'approved',
            'x_tax_line_ids': tax_lines,
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

    def action_view_wht_certificates(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Salary WHT Certificates'),
            'res_model': 'x.wht.certificate',
            'view_mode': 'list,form',
            'domain': [('x_salary_sheet_line_id.sheet_id', '=', self.id)],
        }

    def action_issue_wht_certificates(self):
        """
        Generate one WHT deduction certificate per employee who has WHT > 0.
        Pakistan Income Tax Ordinance 2001 – Section 149 (Salary Tax).
        Each employee is entitled to a certificate showing gross salary and
        tax deducted at source for the tax year.
        """
        self.ensure_one()
        if self.state not in ('approved', 'paid'):
            raise UserError(_(
                'WHT certificates can only be issued for Approved or Paid salary sheets.'))
        if not self.total_wht:
            raise UserError(_('No WHT was deducted on this salary sheet.'))

        WHTCert = self.env['x.wht.certificate']
        tax_period = self._salary_tax_year_label()

        # Find lines that already have a certificate
        existing_line_ids = WHTCert.search([
            ('x_salary_sheet_line_id.sheet_id', '=', self.id)
        ]).mapped('x_salary_sheet_line_id').ids

        new_count = 0
        for line in self.line_ids.filtered(
                lambda l: l.detail_wht > 0 and l.id not in existing_line_ids):
            WHTCert.create({
                'x_salary_sheet_line_id': line.id,
                'wht_amount': line.detail_wht,
                'tax_period': tax_period,
                'issue_date': fields.Date.today(),
                'x_gross_salary': line.basic_salary + line.allowances,
            })
            new_count += 1

        if not new_count:
            raise UserError(_(
                'All WHT certificates already exist for this salary sheet, '
                'or no employees have WHT deducted.'))

        return self.action_view_wht_certificates()

    def _salary_tax_year_label(self):
        """Pakistan tax year runs July – June."""
        if not self.date_from:
            return ''
        y = self.date_from.year
        m = self.date_from.month
        if m >= 7:
            return f'July {y} – June {y + 1}'
        return f'July {y - 1} – June {y}'


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

    detail_basic_full = fields.Monetary(
        string='Monthly Basic', currency_field='currency_id',
        compute='_compute_detail_basic_full', store=False,
        help='Employee full-month contract basic salary (reads from employee record).')
    detail_hra = fields.Monetary(string='HRA', currency_field='currency_id')
    detail_site_allowance = fields.Monetary(
        string='Site Allowance', currency_field='currency_id')
    detail_wht = fields.Monetary(string='WHT', currency_field='currency_id')
    detail_eobi = fields.Monetary(string='EOBI', currency_field='currency_id')
    detail_advance = fields.Monetary(string='Advance Recovery', currency_field='currency_id')

    currency_id = fields.Many2one(
        related='sheet_id.currency_id', depends=['sheet_id'])

    @api.depends('employee_id')
    def _compute_detail_basic_full(self):
        for line in self:
            line.detail_basic_full = line.employee_id.sudo().x_basic_salary or 0.0

    def action_open_slip(self):
        self.ensure_one()
        return self.env.ref(
            'site_operations.action_report_salary_slip').report_action(self)
