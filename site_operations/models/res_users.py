from odoo import models, fields, api


class ResUsersSiteOps(models.Model):
    _inherit = 'res.users'

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
