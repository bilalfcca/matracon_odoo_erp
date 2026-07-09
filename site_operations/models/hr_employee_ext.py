from odoo import models, fields, api, _


class HrEmployeeMatracon(models.Model):
    _inherit = 'hr.employee'

    # ── Last Online ──────────────────────────────────────────────────────────
    # Reads mail.presence.last_presence (updated every ~60 s while the user
    # has an open browser tab) — more granular than login_date (which only
    # changes when the user types their password again).
    # Stored=False: always recomputed at render time, no staleness issue.
    x_last_online = fields.Datetime(
        string='Last Online',
        compute='_compute_x_last_online',
        store=False,
        help='Last time this employee was active in the system. '
             'Only populated when the employee has a linked user account.',
    )
    x_presence_log_ids = fields.One2many(
        'x.employee.presence.log', 'employee_id',
        string='Online/Offline History',
        readonly=True,
    )

    @api.depends('user_id')
    def _compute_x_last_online(self):
        # Batch fetch presences to avoid N+1 queries across list views
        user_ids = [emp.user_id.id for emp in self if emp.user_id]
        if user_ids:
            presences = self.env['mail.presence'].sudo().search(
                [('user_id', 'in', user_ids)]
            )
            presence_map = {p.user_id.id: p.last_presence for p in presences}
        else:
            presence_map = {}
        for emp in self:
            emp.x_last_online = (
                presence_map.get(emp.user_id.id, False) if emp.user_id else False
            )

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
    x_backcharge_balance = fields.Monetary(
        string='Backcharge Balance',
        currency_field='currency_id',
        compute='_compute_backcharge_balance',
        store=False,
        help='Total outstanding backcharge amount across all open/partial records.',
    )
    x_backcharge_count = fields.Integer(
        string='Backcharges',
        compute='_compute_backcharge_balance',
        store=False,
    )
    x_wht_rate = fields.Float(
        string='WHT %',
        help='Default withholding tax percentage for salary.',
    )
    x_eobi_amount = fields.Monetary(
        string='EOBI Deduction',
        currency_field='currency_id',
    )
    x_document_ids = fields.One2many('x.employee.document', 'employee_id', string='Documents')
    currency_id = fields.Many2one(
        related='company_id.currency_id',
        depends=['company_id'],
    )

    def _compute_backcharge_balance(self):
        for emp in self:
            bcs = self.env['x.employee.backcharge'].sudo().search([
                ('employee_id', '=', emp.id),
                ('state', 'in', ('open', 'partial')),
            ])
            emp.x_backcharge_balance = sum(bcs.mapped('remaining_amount'))
            emp.x_backcharge_count = len(bcs)

    def action_view_backcharges(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Employee Backcharges'),
            'res_model': 'x.employee.backcharge',
            'view_mode': 'list,form',
            'domain': [('employee_id', '=', self.id)],
            'context': {'default_employee_id': self.id},
        }

    @api.model_create_multi
    def create(self, vals_list):
        user = self.env.user
        for vals in vals_list:
            if not vals.get('x_project_analytic_account_id'):
                analytic = getattr(user, 'x_default_analytic_account_id', False)
                if analytic:
                    vals['x_project_analytic_account_id'] = analytic.id
        return super().create(vals_list)

    def action_print_employee_card(self):
        return self.env.ref(
            'site_operations.action_report_employee_card'
        ).report_action(self)


class HrEmployeePublicMatracon(models.Model):
    """Expose custom salary fields on the public employee profile
    so non-HR users (e.g. Site Accountants) can read them when
    generating salary slips.

    IMPORTANT: this class must be defined AFTER HrEmployeeMatracon so that
    hr.employee is initialized (and its columns created) before the
    hr_employee_public SQL view is (re)created by init().
    """
    _inherit = 'hr.employee.public'

    x_project_analytic_account_id = fields.Many2one(
        'account.analytic.account', string='Site Project')
    x_cnic = fields.Char(string='CNIC')
    x_basic_salary = fields.Monetary(
        string='Basic Salary', currency_field='currency_id')
    x_hra = fields.Monetary(
        string='House Rent Allowance', currency_field='currency_id')
    x_site_allowance = fields.Monetary(
        string='Site Allowance', currency_field='currency_id')
    x_advance_balance = fields.Monetary(
        string='Advance Balance', currency_field='currency_id')
    x_wht_rate = fields.Float(string='WHT %')
    x_eobi_amount = fields.Monetary(
        string='EOBI Deduction', currency_field='currency_id')
    currency_id = fields.Many2one(
        'res.currency', related='company_id.currency_id',
        depends=['company_id'])
