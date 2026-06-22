from odoo import models, fields


class ResUsersSiteOps(models.Model):
    _inherit = 'res.users'

    x_default_project_id = fields.Many2one(
        'project.project',
        string='Assigned Project',
        readonly=True,
        help='Native Odoo project linked to the user site configuration.',
    )
