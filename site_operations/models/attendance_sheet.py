import calendar
from datetime import date

from markupsafe import Markup

from odoo import models, fields, api, _
from odoo.exceptions import UserError

from . import matracon_notifications as matracon_notify

ATTENDANCE_CODE_SELECTION = [
    ('P', 'Present'),
    ('A', 'Absent'),
    ('L', 'Paid Leave'),
    ('H', 'Public Holiday'),
]


class AttendanceSheet(models.Model):
    _name = 'x.attendance.sheet'
    _description = 'Monthly Attendance Registry'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'date_from desc, id desc'

    name = fields.Char(compute='_compute_name', store=True, readonly=True)
    project_analytic_account_id = fields.Many2one(
        'account.analytic.account', string='Site Project',
        required=True, tracking=True, index=True,
    )
    date_from = fields.Date(required=True, tracking=True)
    date_to = fields.Date(required=True, tracking=True)
    state = fields.Selection([
        ('draft', 'Draft'),
        ('verified', 'Verified'),
        ('posted', 'Posted'),
    ], default='draft', tracking=True, required=True)

    uploaded_file = fields.Binary(string='Uploaded Attendance Sheet')
    uploaded_filename = fields.Char()

    line_ids = fields.One2many(
        'x.attendance.line', 'sheet_id', string='Employees')

    employee_count = fields.Integer(
        compute='_compute_stats', string='Workforce')
    avg_present_pct = fields.Float(
        compute='_compute_stats', string='Monthly Present %')
    total_present_days = fields.Integer(
        compute='_compute_stats', string='Present Days')
    total_absent_days = fields.Integer(
        compute='_compute_stats', string='Absent Days')

    salary_sheet_id = fields.Many2one(
        'x.salary.sheet', string='Salary Sheet', readonly=True, copy=False)

    @api.depends('project_analytic_account_id', 'date_from', 'date_to')
    def _compute_name(self):
        for sheet in self:
            proj = (
                sheet.project_analytic_account_id.code
                or sheet.project_analytic_account_id.name or ''
            )
            month = sheet.date_from.strftime('%b %Y') if sheet.date_from else ''
            sheet.name = f'Attendance/{proj}/{month}' if proj else f'Attendance/{month}'

    @api.depends('line_ids', 'line_ids.present_days', 'line_ids.absent_days',
                 'line_ids.total_days', 'date_from', 'date_to')
    def _compute_stats(self):
        for sheet in self:
            lines = sheet.line_ids
            sheet.employee_count = len(lines)
            sheet.total_present_days = sum(lines.mapped('present_days'))
            sheet.total_absent_days = sum(lines.mapped('absent_days'))
            total_slots = sum(lines.mapped('total_days'))
            sheet.avg_present_pct = (
                (sheet.total_present_days / total_slots * 100.0)
                if total_slots else 0.0
            )

    @api.onchange('date_from')
    def _onchange_date_from(self):
        if self.date_from:
            last_day = calendar.monthrange(
                self.date_from.year, self.date_from.month)[1]
            self.date_to = date(
                self.date_from.year, self.date_from.month, last_day)

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        user = self.env.user
        if not res.get('project_analytic_account_id'):
            analytic = getattr(user, 'x_default_analytic_account_id', False)
            if analytic:
                res['project_analytic_account_id'] = analytic.id
        if res.get('date_from') and not res.get('date_to'):
            d = res['date_from']
            if isinstance(d, str):
                d = fields.Date.from_string(d)
            last = calendar.monthrange(d.year, d.month)[1]
            res['date_to'] = date(d.year, d.month, last)
        return res

    def action_load_employees(self):
        """Populate lines from project employees."""
        for sheet in self:
            if sheet.state != 'draft':
                raise UserError(_('Only draft sheets can be refreshed.'))
            employees = self.env['hr.employee'].search([
                ('x_project_analytic_account_id', '=',
                 sheet.project_analytic_account_id.id),
            ])
            existing = {l.employee_id.id: l for l in sheet.line_ids}
            for emp in employees:
                if emp.id not in existing:
                    self.env['x.attendance.line'].create({
                        'sheet_id': sheet.id,
                        'employee_id': emp.id,
                    })
            sheet.message_post(body=_('Employee list refreshed from project roster.'))

    def action_verify(self):
        for sheet in self:
            if not sheet.line_ids:
                raise UserError(_('Add employees before verifying attendance.'))
            sheet.state = 'verified'
            sheet.message_post(
                body=Markup(_('Attendance verified by <b>%s</b>.')) % self.env.user.name)

    def action_post(self):
        for sheet in self:
            if sheet.state != 'verified':
                raise UserError(_('Verify attendance before posting.'))
            sheet.state = 'posted'
            salary = sheet._generate_salary_sheet()
            sheet.message_post(
                body=Markup(_(
                    'Attendance posted. Salary sheet <b>%s</b> generated.'
                )) % salary.name)
            fo_users = self.env['res.users'].search([
                ('group_ids', 'in', self.env.ref(
                    'site_operations.group_finance_ho').id),
            ])
            matracon_notify.notify_users(
                sheet,
                fo_users,
                _('Attendance posted for <b>%s</b> — salary sheet ready.') % sheet.name,
                summary=_('Attendance Posted'),
            )

    def _generate_salary_sheet(self):
        self.ensure_one()
        existing = self.env['x.salary.sheet'].search([
            ('attendance_sheet_id', '=', self.id),
        ], limit=1)
        if existing:
            return existing
        return self.env['x.salary.sheet'].create({
            'attendance_sheet_id': self.id,
            'project_analytic_account_id': self.project_analytic_account_id.id,
            'date_from': self.date_from,
            'date_to': self.date_to,
        })


class AttendanceLine(models.Model):
    _name = 'x.attendance.line'
    _description = 'Attendance Line'
    _order = 'employee_id'

    sheet_id = fields.Many2one(
        'x.attendance.sheet', required=True, ondelete='cascade', index=True)
    employee_id = fields.Many2one(
        'hr.employee', string='Employee', required=True, index=True)

    present_days = fields.Integer(compute='_compute_day_counts', store=True)
    absent_days = fields.Integer(compute='_compute_day_counts', store=True)
    leave_days = fields.Integer(compute='_compute_day_counts', store=True)
    holiday_days = fields.Integer(compute='_compute_day_counts', store=True)
    paid_days = fields.Integer(compute='_compute_day_counts', store=True)
    total_days = fields.Integer(compute='_compute_day_counts', store=True)
    total_display = fields.Char(compute='_compute_day_counts', store=True)

    @api.depends(
        'sheet_id.date_from', 'sheet_id.date_to',
        *[f'day_{d:02d}' for d in range(1, 32)],
    )
    def _compute_day_counts(self):
        for line in self:
            days_in_month = 0
            if line.sheet_id.date_from:
                days_in_month = calendar.monthrange(
                    line.sheet_id.date_from.year,
                    line.sheet_id.date_from.month,
                )[1]
            present = absent = leave = holiday = 0
            for d in range(1, days_in_month + 1):
                code = getattr(line, f'day_{d:02d}', False)
                if code == 'P':
                    present += 1
                elif code == 'A':
                    absent += 1
                elif code == 'L':
                    leave += 1
                elif code == 'H':
                    holiday += 1
            line.present_days = present
            line.absent_days = absent
            line.leave_days = leave
            line.holiday_days = holiday
            line.paid_days = present + holiday
            line.total_days = days_in_month
            line.total_display = f'{line.paid_days}/{days_in_month}'


for _day in range(1, 32):
    setattr(
        AttendanceLine,
        f'day_{_day:02d}',
        fields.Selection(
            ATTENDANCE_CODE_SELECTION,
            string=str(_day),
        ),
    )
