from odoo import models, fields, api


class HrEmployeeMatracon(models.Model):
    _inherit = 'hr.employee'

    x_project_analytic_account_id = fields.Many2one(
        'account.analytic.account',
        string='Site Project',
        tracking=True,
        index=True,
    )
    x_cnic = fields.Char(string='CNIC', tracking=True)
    x_basic_salary = fields.Monetary(
        string='Basic Salary',
        currency_field='currency_id',
        tracking=True,
    )
    x_hra = fields.Monetary(
        string='House Rent Allowance',
        currency_field='currency_id',
    )
    x_site_allowance = fields.Monetary(
        string='Site Allowance',
        currency_field='currency_id',
    )
    x_advance_balance = fields.Monetary(
        string='Advance Balance',
        currency_field='currency_id',
        help='Outstanding advance to deduct from payroll.',
    )
    x_wht_rate = fields.Float(
        string='WHT %',
        help='Default withholding tax percentage for salary.',
    )
    x_eobi_amount = fields.Monetary(
        string='EOBI Deduction',
        currency_field='currency_id',
    )
    currency_id = fields.Many2one(
        related='company_id.currency_id',
        depends=['company_id'],
    )

    @api.model_create_multi
    def create(self, vals_list):
        user = self.env.user
        for vals in vals_list:
            if not vals.get('x_project_analytic_account_id'):
                analytic = getattr(user, 'x_default_analytic_account_id', False)
                if analytic:
                    vals['x_project_analytic_account_id'] = analytic.id
        return super().create(vals_list)
