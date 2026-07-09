from datetime import timedelta
from odoo import models, fields, api, _


class HrEmployeeMatracon(models.Model):
    _inherit = 'hr.employee'

    # ── Presence fields ──────────────────────────────────────────────────────
    # Both fields are non-stored and computed together from a single
    # mail.presence batch query to avoid N+1 lookups in kanban / list views.
    #
    # ONLINE THRESHOLD: mail.presence.last_presence is updated every ~60 s
    # while the browser tab is open.  We consider a user "currently online"
    # only if their last heartbeat arrived within the past 2 minutes.
    # This prevents the stale-green-dot problem where hr_icon_display keeps
    # the user "online" for up to 5–10 minutes after they close the tab.
    _ONLINE_THRESHOLD_MINUTES = 2

    x_last_online = fields.Datetime(
        string='Last Online',
        compute='_compute_x_presence_info',
        store=False,
        help='Last time this employee had an active browser tab. '
             'Only populated when the employee has a linked user account.',
    )
    x_is_currently_online = fields.Boolean(
        string='Currently Online',
        compute='_compute_x_presence_info',
        store=False,
        help='True when the employee sent a presence heartbeat within the last '
             '2 minutes — more accurate than Odoo\'s built-in hr_icon_display '
             'which can lag by up to 5–10 minutes after logout.',
    )
    x_presence_log_ids = fields.One2many(
        'x.employee.presence.log', 'employee_id',
        string='Online/Offline History',
        readonly=True,
    )

    @api.depends('user_id')
    def _compute_x_presence_info(self):
        """Batch-fetch mail.presence for all employees in the recordset,
        then set both x_last_online and x_is_currently_online in one pass.
        """
        threshold = fields.Datetime.now() - timedelta(
            minutes=self._ONLINE_THRESHOLD_MINUTES
        )
        user_ids = [emp.user_id.id for emp in self if emp.user_id]
        if user_ids:
            presences = self.env['mail.presence'].sudo().search(
                [('user_id', 'in', user_ids)]
            )
            presence_map = {p.user_id.id: p.last_presence for p in presences}
        else:
            presence_map = {}
        for emp in self:
            last = presence_map.get(emp.user_id.id) if emp.user_id else False
            emp.x_last_online = last
            emp.x_is_currently_online = bool(last and last >= threshold)

    def _compute_hr_icon_display(self):
        """Override Odoo's presence dot to use our 2-minute heartbeat threshold.

        Odoo's default implementation reads mail.presence.status which has a
        built-in 5–10 minute grace period — it keeps showing green long after
        the user closes the browser.

        Strategy: let super() run first (sets all the standard values), then
        downgrade any 'presence_online' dot to 'presence_undetermined' (grey)
        for employees whose last_presence heartbeat is older than 2 minutes.
        """
        super()._compute_hr_icon_display()

        threshold = fields.Datetime.now() - timedelta(
            minutes=self._ONLINE_THRESHOLD_MINUTES
        )
        # Only query presences for employees Odoo thinks are online
        online_user_ids = [
            emp.user_id.id for emp in self
            if emp.user_id and emp.hr_icon_display == 'presence_online'
        ]
        if not online_user_ids:
            return

        presences = self.env['mail.presence'].sudo().search(
            [('user_id', 'in', online_user_ids)]
        )
        # Users whose heartbeat has expired
        stale_user_ids = {
            p.user_id.id for p in presences
            if not p.last_presence or p.last_presence < threshold
        }
        # Also treat users with NO presence record as stale
        found_user_ids = {p.user_id.id for p in presences}
        stale_user_ids |= set(online_user_ids) - found_user_ids

        for emp in self:
            if emp.user_id.id in stale_user_ids:
                emp.hr_icon_display = 'presence_undetermined'

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
