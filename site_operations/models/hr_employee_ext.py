from datetime import timedelta

from odoo import models, fields, api, _

# A user is considered online while their last browser heartbeat is within this window.
# 600 s = 10 minutes — user stays green until 10 min of inactivity.
ONLINE_THRESHOLD_SECONDS = 600


class HrEmployeeMatracon(models.Model):
    _inherit = 'hr.employee'

    # ── Rename native 'Absent' label → 'Offline' ────────────────────────────
    # Redefine the selection to change the displayed label in the status badge
    # and kanban dot tooltip. The value 'presence_absent' is unchanged so that
    # Odoo's JS widget and CSS class (o_icon_employee_absent = amber) still work.
    hr_icon_display = fields.Selection(
        selection=[
            ('presence_present', 'Present'),
            ('presence_out_of_working_hour', 'Off-Hours'),
            ('presence_absent', 'Offline'),
            ('presence_archive', 'Archived'),
            ('presence_undetermined', 'Undetermined'),
        ],
        compute='_compute_presence_icon',
        store=False,
    )

    # ── Presence fields ──────────────────────────────────────────────────────
    # x_is_currently_online — True only when last_poll in mail_presence is
    #   within ONLINE_THRESHOLD_SECONDS.  This is the same check Odoo's own
    #   frontend bus uses; reading mail.presence.status is unreliable because
    #   the server never sets it to 'offline' when a browser tab closes.
    # x_last_online — the last_poll timestamp from mail_presence (last heartbeat).
    #   Shown as "Last seen" on the kanban card when the employee is offline.

    x_last_online = fields.Datetime(
        string='Last Seen',
        compute='_compute_x_presence_info',
        store=False,
        help='Last time this employee had an active browser session '
             '(last heartbeat from mail.presence.last_poll). '
             'Only populated when the employee has a linked user account.',
    )
    x_is_currently_online = fields.Boolean(
        string='Currently Online',
        compute='_compute_x_presence_info',
        store=False,
        help='True when the browser sent a heartbeat within the last 65 seconds '
             '(same threshold Odoo\'s own frontend uses).',
    )
    x_presence_log_ids = fields.One2many(
        'x.employee.presence.log', 'employee_id',
        string='Online/Offline History',
        readonly=True,
    )

    @api.depends('user_id')
    def _compute_presence_icon(self):
        """Override Odoo's attendance-based presence dot with browser-heartbeat detection.

        presence_present (green)  — last_poll within ONLINE_THRESHOLD_SECONDS
        presence_absent  (yellow) — heartbeat stale or no presence record
        """
        user_ids = [emp.user_id.id for emp in self if emp.user_id]
        rows = {}
        if user_ids:
            self.env.cr.execute(
                "SELECT user_id, last_poll FROM mail_presence WHERE user_id = ANY(%s)",
                (user_ids,),
            )
            rows = {r[0]: r[1] for r in self.env.cr.fetchall()}

        now = fields.Datetime.now()
        threshold = now - timedelta(seconds=ONLINE_THRESHOLD_SECONDS)

        for emp in self:
            if not emp.user_id:
                emp.hr_icon_display = 'presence_undetermined'
                emp.show_hr_icon_display = False
                continue
            emp.show_hr_icon_display = True
            last_poll = rows.get(emp.user_id.id)
            emp.hr_icon_display = (
                'presence_present'   # green  — browser is open
                if last_poll and last_poll >= threshold
                else 'presence_absent'   # yellow — browser closed / timed out
            )

    @api.depends('user_id')
    def _compute_x_presence_info(self):
        """Batch-read mail_presence.last_poll for all employees in one SQL query.

        Online  = last_poll >= now() - ONLINE_THRESHOLD_SECONDS
        Offline = no presence record, or last_poll older than the threshold

        We use last_poll (not status) because Odoo never writes status='offline'
        server-side — it stays at 'online' in the DB until the GC deletes the
        record after 12 hours, making status unreliable for offline detection.
        """
        user_ids = [emp.user_id.id for emp in self if emp.user_id]
        if not user_ids:
            for emp in self:
                emp.x_last_online = False
                emp.x_is_currently_online = False
            return

        self.env.cr.execute(
            "SELECT user_id, last_poll FROM mail_presence WHERE user_id = ANY(%s)",
            (user_ids,)
        )
        rows = {r[0]: r[1] for r in self.env.cr.fetchall()}

        now = fields.Datetime.now()
        threshold = now - timedelta(seconds=ONLINE_THRESHOLD_SECONDS)

        for emp in self:
            uid = emp.user_id.id if emp.user_id else False
            last_poll = rows.get(uid) if uid else None
            emp.x_is_currently_online = bool(last_poll and last_poll >= threshold)
            emp.x_last_online = last_poll or False

    # ── Cron: detect offline transitions ────────────────────────────────────

    @api.model
    def _cron_update_presence_log(self):
        """Runs every minute — closes open sessions for employees whose heartbeat
        has gone stale (last_poll older than ONLINE_THRESHOLD_SECONDS).

        Odoo never sends an 'offline' signal when a browser tab closes, so this
        cron is the only way to detect the disconnect.

        offline_at is stamped at last_poll (the final heartbeat received) —
        the closest we can get to the real disconnect time without client cooperation.
        """
        employees = self.search([('user_id', '!=', False)])
        if not employees:
            return

        user_ids = employees.mapped('user_id').ids
        self.env.cr.execute(
            "SELECT user_id, last_poll FROM mail_presence WHERE user_id = ANY(%s)",
            (user_ids,),
        )
        presence_map = {r[0]: r[1] for r in self.env.cr.fetchall()}

        now = fields.Datetime.now()
        threshold = now - timedelta(seconds=ONLINE_THRESHOLD_SECONDS)

        Session = self.env['x.employee.presence.log'].sudo()

        for emp in employees:
            uid = emp.user_id.id
            last_poll = presence_map.get(uid)

            # Still online — nothing to do
            if last_poll and last_poll >= threshold:
                continue

            # Heartbeat is stale → close any open session
            open_session = Session.search([
                ('employee_id', '=', emp.id),
                ('offline_at', '=', False),
            ], limit=1)
            if open_session:
                open_session.offline_at = last_poll or now

    # ── Other fields / methods ───────────────────────────────────────────────

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
