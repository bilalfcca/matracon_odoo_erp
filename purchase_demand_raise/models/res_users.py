from odoo import models, fields


class ResUsers(models.Model):
    _inherit = 'res.users'

    # Set automatically by x.project.site.config when user is added/removed.
    # Head Office users: all fields are empty (they see all projects).
    # Site users: all fields are set from their assigned project.

    x_site_config_id = fields.Many2one(
        'x.project.site.config',
        string='Assigned Site Project',
        readonly=True,
        help='Automatically set when the user is added to a Site Project Configuration.',
    )
    x_default_analytic_account_id = fields.Many2one(
        'account.analytic.account',
        string='Project Analytic Account',
        readonly=True,
        help='Automatically derived from the assigned Site Project Configuration.',
    )
    x_default_warehouse_id = fields.Many2one(
        'stock.warehouse',
        string='Project Warehouse',
        readonly=True,
        help='Automatically derived from the assigned Site Project Configuration. '
             'Used as the default "Deliver To" warehouse on new Purchase Requisitions.',
    )
