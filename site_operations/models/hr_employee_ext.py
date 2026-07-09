from odoo import models, fields, api, _


class HrEmployeeMatracon(models.Model):
    _inherit = 'hr.employee'

    # ── Presence fields ──────────────────────────────────────────────────────
    # Odoo's bus/WebSocket system natively manages mail.presence.status:
    #   'online'  → browser tab is open and connected
    #   'away'    → tab open but idle
    #   'offline' → browser closed / disconnected
    # We read that status directly — no manual time thresholds needed.
    # x_last_online  = last_presence timestamp (shown as "Online From: …")
    # x_is_currently_online = True only when Odoo's native status == 'online'

    x_last_online = fields.Datetime(
        string='Last Online',
        compute='_compute_x_presence_info',
        store=False,
        help='Last time this employee had an active browser session. '
             'Only populated when the employee has a linked user account.',
    )
    x_is_currently_online = fields.Boolean(
        string='Currently Online',
        compute='_compute_x_presence_info',
        store=False,
        help='True when Odoo\'s native presence status is "online" '
             '(browser tab open and connected).',
    )
    x_presence_log_ids = fields.One2many(
        'x.employee.presence.log', 'employee_id',
        string='Online/Offline History',
        readonly=True,
    )

    @api.depends('user_id')
    def _compute_x_presence_info(self):
        """Batch-fetch mail.presence for all employees in the recordset.

        Reads two fields from mail.presence in a single query:
          • last_presence  → shown as "Online From: …" in the kanban card
          • status         → drives x_is_currently_online (trusts Odoo's bus)

        No manual time-threshold logic — Odoo's WebSocket/bus already handles
        the online→offline transition when the browser tab closes.
        """
        user_ids = [emp.user_id.id for emp in self if emp.user_id]
        if user_ids:
            presences = self.env['mail.presence'].sudo().search(
                [('user_id', 'in', user_ids)]
            )
            last_map = {p.user_id.id: p.last_presence for p in presences}
            status_map = {p.user_id.id: p.status for p in presences}
        else:
            last_map = {}
            status_map = {}

        for emp in self:
            uid = emp.user_id.id if emp.user_id else False
            emp.x_last_online = last_map.get(uid, False)
            emp.x_is_currently_online = (status_map.get(uid) == 'online')

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
