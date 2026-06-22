from odoo import models, fields, api


class ProjectSiteConfigAccountant(models.Model):
    _inherit = 'x.project.site.config'

    x_site_accountant_ids = fields.Many2many(
        'res.users',
        'site_config_accountant_rel',
        'config_id', 'user_id',
        string='Site Accountants',
        help=(
            'Site Accountant users for this project. '
            'Adding a user here automatically:\n'
            '  \u2022 Assigns them to the Site Accountant security group\n'
            '  \u2022 Sets their default analytic account to this project\n'
            '  \u2022 Sets their default warehouse to this project\'s warehouse\n'
            'Removing a user reverses these automatically.'
        ),
    )
    x_accountant_count = fields.Integer(
        compute='_compute_accountant_count',
        string='Accountants',
    )

    def _compute_accountant_count(self):
        for config in self:
            config.x_accountant_count = len(config.x_site_accountant_ids)

    def write(self, vals):
        # Capture old accountant sets before the write
        old_accountant_sets = {config.id: set(config.x_site_accountant_ids.ids) for config in self}
        res = super().write(vals)
        if 'x_site_accountant_ids' in vals:
            for config in self:
                old_accountants = old_accountant_sets.get(config.id, set())
                new_accountants = set(config.x_site_accountant_ids.ids)
                added_ids = new_accountants - old_accountants
                removed_ids = old_accountants - new_accountants
                if added_ids:
                    config._assign_accountants(self.env['res.users'].browse(list(added_ids)))
                if removed_ids:
                    config._unassign_accountants(self.env['res.users'].browse(list(removed_ids)))
        return res

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        for record in records:
            if record.x_site_accountant_ids:
                record._assign_accountants(record.x_site_accountant_ids)
        return records

    def _assign_accountants(self, users):
        """
        When accountant users are added to this site config:
        1. Add them to the Site Accountant security group
        2. Set their default analytic account = this project's analytic account
        3. Set their default warehouse = this project's warehouse
        4. Set their x_site_config_id = this config
        """
        site_accountant_group = self.env.ref(
            'site_operations.group_site_accountant', raise_if_not_found=False
        )
        for user in users:
            vals = {
                'x_default_analytic_account_id': self.analytic_account_id.id,
                'x_default_warehouse_id': self.warehouse_id.id if self.warehouse_id else False,
                'x_site_config_id': self.id,
            }
            if site_accountant_group:
                vals['group_ids'] = [(4, site_accountant_group.id)]
            user.write(vals)

    def _unassign_accountants(self, users):
        """
        When accountant users are removed from this site config:
        1. Check if they belong to another site config (as accountant or store user)
        2. If not -> remove from Site Accountant group and clear defaults
        3. If yes -> update to point to the other config instead
        """
        site_accountant_group = self.env.ref(
            'site_operations.group_site_accountant', raise_if_not_found=False
        )
        for user in users:
            # Check if the user is assigned to another site config as accountant
            other_config = self.search([
                ('x_site_accountant_ids', 'in', user.id),
                ('id', '!=', self.id),
            ], limit=1)
            if other_config:
                # Reassign to the other config
                user.write({
                    'x_default_analytic_account_id': other_config.analytic_account_id.id,
                    'x_default_warehouse_id': other_config.warehouse_id.id if other_config.warehouse_id else False,
                    'x_site_config_id': other_config.id,
                })
            else:
                # No other config — fully unassign
                unwrite_vals = {
                    'x_default_analytic_account_id': False,
                    'x_default_warehouse_id': False,
                    'x_site_config_id': False,
                }
                if site_accountant_group:
                    unwrite_vals['group_ids'] = [(3, site_accountant_group.id)]
                user.write(unwrite_vals)

    def action_view_accountants(self):
        self.ensure_one()
        from odoo import _
        return {
            'type': 'ir.actions.act_window',
            'name': _('Site Accountants \u2014 %s') % self.name,
            'res_model': 'res.users',
            'view_mode': 'list,form',
            'domain': [('id', 'in', self.x_site_accountant_ids.ids)],
            'context': {},
        }
