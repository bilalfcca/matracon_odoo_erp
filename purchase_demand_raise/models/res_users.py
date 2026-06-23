from odoo import models, fields, api


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

    # ── Matracon role helpers (use groups, not hard-coded user IDs) ─────────

    def _matracon_is_admin(self):
        self.ensure_one()
        return self.user_has_groups(
            'site_operations.group_matracon_admin,base.group_system'
        )

    def _matracon_is_head_office(self):
        self.ensure_one()
        return self.user_has_groups(
            'purchase_demand_raise.group_head_office,'
            'site_operations.group_matracon_admin,base.group_system'
        )

    def _matracon_is_procurement_officer(self):
        self.ensure_one()
        return self.user_has_groups(
            'purchase_demand_raise.group_procurement_ho,'
            'site_operations.group_matracon_admin,base.group_system'
        )

    def _matracon_is_ceo(self):
        self.ensure_one()
        return self.user_has_groups(
            'purchase_demand_raise.group_ceo_approval,'
            'site_operations.group_matracon_admin,base.group_system'
        )

    def _matracon_is_finance_officer(self):
        self.ensure_one()
        return self.user_has_groups(
            'site_operations.group_finance_ho,'
            'site_operations.group_matracon_admin,base.group_system'
        )

    def _matracon_is_site_store(self):
        self.ensure_one()
        return self.user_has_groups(
            'purchase_demand_raise.group_site_store,'
            'site_operations.group_matracon_admin,base.group_system'
        )

    @api.model
    def _matracon_add_group(self, user, group):
        """Idempotently add a security group to a user (Odoo 19: group_ids)."""
        if not group or not user:
            return
        user = user.sudo()
        if group.id not in user.group_ids.ids:
            user.write({'group_ids': [(4, group.id)]})

    @api.model
    def _matracon_remove_group(self, user, group):
        """Remove a directly-assigned security group from a user."""
        if not group or not user:
            return
        user = user.sudo()
        if group.id in user.group_ids.ids:
            user.write({'group_ids': [(3, group.id)]})

