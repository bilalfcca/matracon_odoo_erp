"""Leave management for site employees, run by the Site Accountant.

* x.site.leave.type        — leave types with yearly entitlement and paid/unpaid
* x.site.leave.allocation  — yearly balance per employee and type (editable)
* x.site.leave             — leave applications; approved by the Site Accountant
                             with the Project Manager's confirmation document

Approved leave is written into the monthly Attendance Registry
(x.attendance.sheet) as 'L' (paid) or 'U' (unpaid) on each leave day.
"""
from datetime import date, timedelta

from markupsafe import Markup

from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError


class SiteLeaveType(models.Model):
    _name = 'x.site.leave.type'
    _description = 'Site Leave Type'
    _order = 'sequence, id'

    name = fields.Char(required=True, translate=True)
    sequence = fields.Integer(default=10)
    days_per_year = fields.Float(
        string='Days per Year', default=0.0,
        help='Yearly entitlement per employee. 0 = no limit (no balance check).')
    is_paid = fields.Boolean(
        string='Paid', default=True,
        help='Paid leave is marked "L" in the Attendance Registry and paid in salary. '
             'Unpaid leave is marked "U" and not paid.')
    requires_pm_document = fields.Boolean(
        string='PM Confirmation Required', default=True,
        help='A document confirming the Project Manager\'s approval must be '
             'uploaded before the Site Accountant can approve the leave.')
    active = fields.Boolean(default=True)

    @property
    def attendance_code(self):
        self.ensure_one()
        return 'L' if self.is_paid else 'U'


class SiteLeaveAllocation(models.Model):
    _name = 'x.site.leave.allocation'
    _description = 'Site Leave Balance'
    _order = 'year desc, employee_id, leave_type_id'
    _rec_name = 'employee_id'

    employee_id = fields.Many2one('hr.employee', required=True, index=True, ondelete='cascade')
    project_analytic_account_id = fields.Many2one(
        'account.analytic.account', string='Site Project', index=True,
        compute='_compute_project', store=True, readonly=False, precompute=True)
    leave_type_id = fields.Many2one('x.site.leave.type', required=True, ondelete='restrict')
    year = fields.Integer(required=True, default=lambda self: fields.Date.context_today(self).year)
    entitled_days = fields.Float(
        string='Entitled Days', compute='_compute_entitled_days', store=True,
        readonly=False, precompute=True,
        help='Defaults to the leave type\'s yearly days; adjust for this employee if needed.')
    taken_days = fields.Float(string='Taken', compute='_compute_balance')
    remaining_days = fields.Float(string='Remaining', compute='_compute_balance')
    note = fields.Char(string='Adjustment Note')

    _unique_balance = models.Constraint(
        'UNIQUE(employee_id, leave_type_id, year)',
        'There is already a leave balance for this employee, leave type and year.')

    @api.depends('employee_id')
    def _compute_project(self):
        for rec in self:
            if not rec.project_analytic_account_id:
                rec.project_analytic_account_id = rec.employee_id.x_project_analytic_account_id

    @api.depends('leave_type_id')
    def _compute_entitled_days(self):
        for rec in self:
            if not rec.entitled_days:
                rec.entitled_days = rec.leave_type_id.days_per_year

    def _compute_balance(self):
        Leave = self.env['x.site.leave'].sudo()
        for rec in self:
            taken = 0.0
            if rec.employee_id and rec.leave_type_id and rec.year:
                taken = sum(Leave.search([
                    ('employee_id', '=', rec.employee_id.id),
                    ('leave_type_id', '=', rec.leave_type_id.id),
                    ('state', '=', 'approved'),
                    ('date_from', '>=', date(rec.year, 1, 1)),
                    ('date_from', '<=', date(rec.year, 12, 31)),
                ]).mapped('number_of_days'))
            rec.taken_days = taken
            rec.remaining_days = rec.entitled_days - taken

    @api.model
    def _get_or_create(self, employee, leave_type, year):
        alloc = self.sudo().search([
            ('employee_id', '=', employee.id),
            ('leave_type_id', '=', leave_type.id),
            ('year', '=', year),
        ], limit=1)
        if not alloc:
            alloc = self.sudo().create({
                'employee_id': employee.id,
                'leave_type_id': leave_type.id,
                'year': year,
                'project_analytic_account_id': employee.x_project_analytic_account_id.id,
            })
        return alloc

    def action_generate_current_year(self):
        """Create this year's balances for every site employee the user can see."""
        year = fields.Date.context_today(self).year
        types = self.env['x.site.leave.type'].search([('days_per_year', '>', 0)])
        domain = [('x_project_analytic_account_id', '!=', False)]
        if self.env.user.has_group('site_operations.group_site_accountant') and not (
            self.env.user.has_group('site_operations.group_finance_ho')
            or self.env.user.has_group('purchase_demand_raise.group_head_office')
            or self.env.user.has_group('purchase_demand_raise.group_matracon_admin')
        ):
            domain = [('x_project_analytic_account_id', '=',
                       self.env.user.x_default_analytic_account_id.id)]
        employees = self.env['hr.employee'].search(domain)
        created = 0
        for emp in employees:
            for ltype in types:
                exists = self.sudo().search_count([
                    ('employee_id', '=', emp.id), ('leave_type_id', '=', ltype.id),
                    ('year', '=', year)])
                if not exists:
                    self.create({
                        'employee_id': emp.id, 'leave_type_id': ltype.id, 'year': year,
                        'project_analytic_account_id': emp.x_project_analytic_account_id.id,
                    })
                    created += 1
        return {
            'type': 'ir.actions.client', 'tag': 'display_notification',
            'params': {
                'title': _('Leave Balances %s') % year,
                'message': _('%s new balance(s) created.') % created,
                'type': 'success', 'sticky': False,
                'next': {'type': 'ir.actions.client', 'tag': 'soft_reload'},
            },
        }


class SiteLeave(models.Model):
    _name = 'x.site.leave'
    _description = 'Site Leave Application'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'date_from desc, id desc'

    name = fields.Char(compute='_compute_name', store=True)
    employee_id = fields.Many2one(
        'hr.employee', string='Employee', required=True, index=True, tracking=True,
        domain="[('x_project_analytic_account_id', '!=', False)]")
    project_analytic_account_id = fields.Many2one(
        'account.analytic.account', string='Site Project', index=True,
        compute='_compute_project', store=True, readonly=False, precompute=True)
    leave_type_id = fields.Many2one(
        'x.site.leave.type', string='Leave Type', required=True, tracking=True,
        ondelete='restrict')
    date_from = fields.Date(string='From', required=True, tracking=True)
    date_to = fields.Date(string='To', required=True, tracking=True)
    number_of_days = fields.Float(
        string='Days', compute='_compute_number_of_days', store=True, readonly=False,
        precompute=True, tracking=True,
        help='Calendar days from From to To. Adjust if some days should not count.')
    reason = fields.Text()
    pm_document = fields.Binary(string='PM Confirmation', attachment=True)
    pm_document_filename = fields.Char()
    requires_pm_document = fields.Boolean(related='leave_type_id.requires_pm_document')
    state = fields.Selection([
        ('draft', 'Draft'),
        ('approved', 'Approved'),
        ('rejected', 'Rejected'),
        ('cancelled', 'Cancelled'),
    ], default='draft', required=True, tracking=True, copy=False)
    approved_by_id = fields.Many2one('res.users', string='Approved By', readonly=True, copy=False)
    approved_date = fields.Datetime(string='Approved On', readonly=True, copy=False)
    remaining_days = fields.Float(
        string='Balance Remaining', compute='_compute_remaining_days',
        help='Remaining balance for this leave type in the leave year (before this leave).')

    @api.depends('employee_id', 'leave_type_id', 'date_from')
    def _compute_name(self):
        for rec in self:
            parts = [rec.employee_id.name or '', rec.leave_type_id.name or '']
            if rec.date_from:
                parts.append(rec.date_from.strftime('%d %b %Y'))
            rec.name = ' / '.join(p for p in parts if p) or _('New Leave')

    @api.depends('employee_id')
    def _compute_project(self):
        for rec in self:
            if rec.employee_id:
                rec.project_analytic_account_id = rec.employee_id.x_project_analytic_account_id

    @api.depends('date_from', 'date_to')
    def _compute_number_of_days(self):
        for rec in self:
            if rec.date_from and rec.date_to and rec.date_to >= rec.date_from:
                rec.number_of_days = (rec.date_to - rec.date_from).days + 1
            else:
                rec.number_of_days = 0.0

    @api.depends('employee_id', 'leave_type_id', 'date_from', 'state')
    def _compute_remaining_days(self):
        for rec in self:
            remaining = 0.0
            if rec.employee_id and rec.leave_type_id and rec.date_from:
                alloc = self.env['x.site.leave.allocation'].sudo().search([
                    ('employee_id', '=', rec.employee_id.id),
                    ('leave_type_id', '=', rec.leave_type_id.id),
                    ('year', '=', rec.date_from.year),
                ], limit=1)
                if alloc:
                    remaining = alloc.remaining_days
                    if rec.state == 'approved':
                        remaining += rec.number_of_days
                else:
                    remaining = rec.leave_type_id.days_per_year
            rec.remaining_days = remaining

    @api.constrains('date_from', 'date_to', 'number_of_days')
    def _check_dates(self):
        for rec in self:
            if rec.date_from and rec.date_to and rec.date_to < rec.date_from:
                raise ValidationError(_('"To" date cannot be before "From" date.'))
            if rec.number_of_days < 0:
                raise ValidationError(_('Number of days cannot be negative.'))

    _LOCKED_FIELDS = {'employee_id', 'leave_type_id', 'date_from', 'date_to', 'number_of_days'}

    def write(self, vals):
        if self._LOCKED_FIELDS & set(vals) and not self.env.context.get('_site_leave_internal'):
            locked = self.filtered(lambda r: r.state == 'approved')
            if locked:
                raise UserError(_(
                    'Approved leave cannot be changed. Use "Reset to Draft" first, '
                    'edit it, then approve again.'))
        return super().write(vals)

    def unlink(self):
        if self.filtered(lambda r: r.state == 'approved'):
            raise UserError(_('Cancel the leave before deleting it.'))
        return super().unlink()

    # ── Workflow ──────────────────────────────────────────────────────────────
    def action_approve(self):
        for rec in self:
            if rec.state != 'draft':
                raise UserError(_('Only draft leave applications can be approved.'))
            if rec.leave_type_id.requires_pm_document and not rec.pm_document:
                raise UserError(_(
                    'Upload the Project Manager\'s confirmation document before '
                    'approving this leave.'))
            if not rec.number_of_days:
                raise UserError(_('The leave has 0 days.'))
            overlap = self.search([
                ('id', '!=', rec.id), ('employee_id', '=', rec.employee_id.id),
                ('state', '=', 'approved'),
                ('date_from', '<=', rec.date_to), ('date_to', '>=', rec.date_from),
            ], limit=1)
            if overlap:
                raise UserError(_(
                    '%(emp)s already has approved leave overlapping these dates: %(leave)s'
                ) % {'emp': rec.employee_id.name, 'leave': overlap.name})
            ltype = rec.leave_type_id
            if ltype.days_per_year > 0:
                Alloc = self.env['x.site.leave.allocation'].sudo()
                alloc = Alloc.search([
                    ('employee_id', '=', rec.employee_id.id),
                    ('leave_type_id', '=', ltype.id),
                    ('year', '=', rec.date_from.year)], limit=1)
                remaining = alloc.remaining_days if alloc else ltype.days_per_year
                if rec.number_of_days > remaining + 1e-6:
                    raise UserError(_(
                        'Not enough %(type)s balance for %(emp)s in %(year)s: '
                        '%(rem)s day(s) remaining, %(req)s requested.\n\n'
                        'To grant it anyway, add or adjust this employee\'s balance under '
                        'Leave Balances (Entitled Days), or use Unpaid Leave.'
                    ) % {'type': ltype.name, 'emp': rec.employee_id.name,
                         'year': rec.date_from.year,
                         'rem': round(remaining, 2),
                         'req': rec.number_of_days})
                if not alloc:
                    Alloc._get_or_create(rec.employee_id, ltype, rec.date_from.year)
            rec.with_context(_site_leave_internal=True).write({
                'state': 'approved',
                'approved_by_id': self.env.user.id,
                'approved_date': fields.Datetime.now(),
            })
            rec._apply_to_registry(mark=True)

    def action_reject(self):
        for rec in self:
            if rec.state != 'draft':
                raise UserError(_('Only draft leave applications can be rejected.'))
            rec.state = 'rejected'

    def action_cancel(self):
        for rec in self:
            if rec.state == 'approved':
                rec._apply_to_registry(mark=False)
            rec.with_context(_site_leave_internal=True).write({
                'state': 'cancelled', 'approved_by_id': False, 'approved_date': False})

    def action_reset_to_draft(self):
        for rec in self:
            if rec.state == 'approved':
                rec._apply_to_registry(mark=False)
            rec.with_context(_site_leave_internal=True).write({
                'state': 'draft', 'approved_by_id': False, 'approved_date': False})

    # ── Attendance Registry integration ───────────────────────────────────────
    def _leave_dates(self):
        self.ensure_one()
        d = self.date_from
        while d and d <= self.date_to:
            yield d
            d += timedelta(days=1)

    def _apply_to_registry(self, mark=True):
        """Write (or remove) this leave's code on the employee's draft
        Attendance Registry rows. Verified/posted sheets are never changed."""
        Sheet = self.env['x.attendance.sheet'].sudo()
        for rec in self:
            code = rec.leave_type_id.attendance_code
            project = rec.project_analytic_account_id or rec.employee_id.x_project_analytic_account_id
            updated, skipped = set(), set()
            for d in rec._leave_dates():
                sheet = Sheet.search([
                    ('project_analytic_account_id', '=', project.id),
                    ('date_from', '<=', d), ('date_to', '>=', d),
                ], limit=1)
                if not sheet:
                    continue
                if sheet.state != 'draft':
                    skipped.add(sheet.name)
                    continue
                line = sheet.line_ids.filtered(lambda l: l.employee_id == rec.employee_id)[:1]
                if not line:
                    continue
                fname = 'day_%02d' % d.day
                if mark:
                    line[fname] = code
                elif line[fname] == code:
                    line[fname] = False
                updated.add(sheet.name)
            if updated:
                rec.message_post(body=_('%(action)s Attendance Registry: %(sheets)s') % {
                    'action': _('Marked on') if mark else _('Removed from'),
                    'sheets': ', '.join(sorted(updated))})
            if skipped:
                rec.message_post(body=Markup(_(
                    '<b>Not applied</b> to verified/posted registry: %s. '
                    'Update those days manually if needed.')) % ', '.join(sorted(skipped)))

    @api.model
    def _apply_approved_to_sheet(self, sheet):
        """Mark all approved leave of the sheet's month on a draft sheet."""
        if sheet.state != 'draft':
            return
        leaves = self.sudo().search([
            ('project_analytic_account_id', '=', sheet.project_analytic_account_id.id),
            ('state', '=', 'approved'),
            ('date_from', '<=', sheet.date_to), ('date_to', '>=', sheet.date_from),
        ])
        for leave in leaves:
            line = sheet.line_ids.filtered(lambda l: l.employee_id == leave.employee_id)[:1]
            if not line:
                continue
            code = leave.leave_type_id.attendance_code
            for d in leave._leave_dates():
                if sheet.date_from <= d <= sheet.date_to:
                    line['day_%02d' % d.day] = code
        return len(leaves)
