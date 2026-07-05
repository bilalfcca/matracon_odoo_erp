from odoo import models, fields, api, _
from odoo.exceptions import UserError


class EmployeeBackcharge(models.Model):
    """Asset-based backcharge raised against an employee.

    Created by the site accountant when an issued item is damaged or lost.
    The outstanding balance feeds into the salary sheet each month as a
    separate deduction item — distinct from salary advances.
    """
    _name = 'x.employee.backcharge'
    _description = 'Employee Backcharge'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'backcharge_date desc, id desc'

    name = fields.Char(
        string='Reference', readonly=True, copy=False, default='New')

    employee_id = fields.Many2one(
        'hr.employee', required=True, string='Employee',
        tracking=True, index=True)

    project_analytic_account_id = fields.Many2one(
        'account.analytic.account', string='Site Project',
        tracking=True,
        default=lambda self: self.env.user.sudo().x_default_analytic_account_id,
    )

    description = fields.Char(
        string='Description / Asset', required=True, tracking=True,
        help='What was issued and the reason for the backcharge '
             '(e.g. "Hard hat — lost", "Safety harness — damaged").')

    issue_date = fields.Date(
        string='Issue Date',
        help='Date the asset/item was originally issued to the employee.')

    backcharge_date = fields.Date(
        string='Backcharge Date', required=True,
        default=fields.Date.today,
        help='Date the backcharge was raised (damage or loss assessment).')

    backcharge_amount = fields.Monetary(
        string='Backcharge Amount', required=True,
        currency_field='currency_id', tracking=True,
        help='Total amount to be recovered from the employee.')

    recovered_amount = fields.Monetary(
        string='Recovered', currency_field='currency_id',
        compute='_compute_recovered', store=True,
        help='Total amount recovered so far via salary deductions.')

    remaining_amount = fields.Monetary(
        string='Remaining', currency_field='currency_id',
        compute='_compute_recovered', store=True,
        help='Amount still outstanding (to be deducted in future months).')

    state = fields.Selection([
        ('open',      'Open'),
        ('partial',   'Partially Recovered'),
        ('cleared',   'Cleared'),
        ('cancelled', 'Cancelled'),
    ], default='open', tracking=True, required=True)

    notes = fields.Text(string='Notes / Reason')

    currency_id = fields.Many2one(
        'res.currency',
        default=lambda self: self.env.company.currency_id)

    deduction_ids = fields.One2many(
        'x.employee.backcharge.deduction', 'backcharge_id',
        string='Deduction History', readonly=True)

    # ─────────────────────────────────────────────────────────────────────────

    @api.model_create_multi
    def create(self, vals_list):
        Analytic = self.env['account.analytic.account']
        for vals in vals_list:
            if vals.get('name', 'New') == 'New':
                site_code = Analytic._matracon_site_code_for_id(
                    vals.get('project_analytic_account_id'))
                vals['name'] = Analytic._matracon_ref_with_site(
                    'x.employee.backcharge', site_code)
        return super().create(vals_list)

    @api.depends('deduction_ids.amount')
    def _compute_recovered(self):
        for bc in self:
            recovered = sum(bc.deduction_ids.mapped('amount'))
            bc.recovered_amount = recovered
            bc.remaining_amount = max(bc.backcharge_amount - recovered, 0.0)

    def action_cancel(self):
        for bc in self:
            if bc.state == 'cleared':
                raise UserError(_('A cleared backcharge cannot be cancelled.'))
            bc.state = 'cancelled'

    def action_reopen(self):
        for bc in self:
            if bc.state != 'cancelled':
                raise UserError(_('Only cancelled backcharges can be re-opened.'))
            bc.state = 'open' if bc.remaining_amount > 0 else 'cleared'


class EmployeeBackchargeDeduction(models.Model):
    """One record per salary-sheet deduction for a backcharge.

    Created automatically when the CEO approves a salary sheet that
    contains a backcharge_to_deduct amount.
    """
    _name = 'x.employee.backcharge.deduction'
    _description = 'Employee Backcharge Monthly Deduction'
    _order = 'id desc'

    backcharge_id = fields.Many2one(
        'x.employee.backcharge', required=True,
        ondelete='cascade', index=True)

    salary_line_id = fields.Many2one(
        'x.salary.sheet.line', string='Salary Line',
        readonly=True, ondelete='set null')

    salary_sheet_id = fields.Many2one(
        'x.salary.sheet',
        related='salary_line_id.sheet_id',
        string='Salary Sheet', store=True, readonly=True)

    amount = fields.Monetary(
        string='Deducted Amount', currency_field='currency_id')

    deduction_date = fields.Date(string='Deduction Date')

    currency_id = fields.Many2one(
        'res.currency',
        related='backcharge_id.currency_id', store=True)
