from odoo import models, fields, api


class ResUsersSiteOps(models.Model):
    _inherit = 'res.users'

    # ── Auto-create employee on user creation ────────────────────────────────

    @api.model_create_multi
    def create(self, vals_list):
        users = super().create(vals_list)
        Employee = self.env['hr.employee'].sudo()
        for user in users:
            # Skip portal / public users — only internal users get an employee record
            if user.share:
                continue
            # Skip if an employee is already linked (e.g. created via HR module)
            if Employee.search_count([('user_id', '=', user.id)]):
                continue
            Employee.create({
                'name': user.name,
                'user_id': user.id,
                'work_email': user.email or user.login,
                'company_id': user.company_id.id,
            })
        return users

    # ── Last Online ──────────────────────────────────────────────────────────
    # Reads mail.presence.last_presence (updated every ~60 s while the user
    # has an open browser tab) — more accurate than login_date which only
    # updates on password re-entry.  Shown on the Users form (Preferences tab)
    # and on the employee list / kanban views via hr.employee.x_last_online.
    x_last_online = fields.Datetime(
        string='Last Online',
        compute='_compute_x_last_online',
        store=False,
        help='Last time this user was active in the system. '
             'Updated every ~60 s while a browser tab is open.',
    )

    def _compute_x_last_online(self):
        presences = self.env['mail.presence'].sudo().search(
            [('user_id', 'in', self.ids)]
        )
        presence_map = {p.user_id.id: p.last_presence for p in presences}
        for user in self:
            user.x_last_online = presence_map.get(user.id, False)

    x_default_project_id = fields.Many2one(
        'project.project',
        string='Assigned Project',
        readonly=True,
        help='Native Odoo project linked to the user site configuration.',
    )

    matracon_app_group_ids = fields.Many2many(
        'res.groups',
        compute='_compute_matracon_app_group_ids',
        string='Matracon App Groups',
    )

    @api.depends()
    def _compute_matracon_app_group_ids(self):
        all_app = self.env['res.groups']
        for xml_id in (
            'group_mtr_app_purchase',
            'group_mtr_app_inventory',
            'group_mtr_app_accounting',
            'group_mtr_app_project',
            'group_mtr_app_hr',
            'group_mtr_app_attendance',
            'group_mtr_app_payroll',
            'group_mtr_app_settings',
        ):
            g = self.env.ref(f'site_operations.{xml_id}', raise_if_not_found=False)
            if g:
                all_app |= g
        for user in self:
            user.matracon_app_group_ids = all_app
