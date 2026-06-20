from odoo import models, fields, api, _
from odoo.exceptions import ValidationError


class ProjectSiteConfig(models.Model):
    _name = 'x.project.site.config'
    _description = 'Site Project Configuration'
    _inherit = ['mail.thread']
    _rec_name = 'name'
    _order = 'name'

    name = fields.Char('Project Name', required=True, tracking=True)
    analytic_account_id = fields.Many2one(
        'account.analytic.account',
        string='Analytic Account',
        required=True,
        tracking=True,
        help='The analytic account for this project. Auto-assigned to all PRs created by site users of this project.',
    )
    warehouse_id = fields.Many2one(
        'stock.warehouse',
        string='Site Warehouse',
        tracking=True,
        help='The warehouse for this project site. Used as the default "Deliver To" on new Purchase Requisitions raised by site users.',
    )
    site_user_ids = fields.Many2many(
        'res.users',
        'x_project_site_user_rel',
        'config_id', 'user_id',
        string='Site Users',
        help=(
            'All site-level users (Store Keepers, Site Accountants) for this project. '
            'Adding a user here automatically:\n'
            '  \u2022 Assigns them to the Site Store security group\n'
            '  \u2022 Sets their default analytic account to this project\n'
            'Removing a user reverses these automatically.'
        ),
    )
    user_count = fields.Integer(compute='_compute_user_count', string='Users')
    active = fields.Boolean(default=True)

    def _compute_user_count(self):
        for config in self:
            config.user_count = len(config.site_user_ids)

    @api.constrains('analytic_account_id')
    def _check_unique_analytic(self):
        for config in self:
            duplicate = self.search([
                ('analytic_account_id', '=', config.analytic_account_id.id),
                ('id', '!=', config.id),
            ], limit=1)
            if duplicate:
                raise ValidationError(
                    _('Analytic account "%s" is already used by project "%s". Each project must have a unique analytic account.')
                    % (config.analytic_account_id.name, duplicate.name)
                )

    def write(self, vals):
        # Capture the old user sets before the write
        old_user_sets = {config.id: set(config.site_user_ids.ids) for config in self}
        res = super().write(vals)
        if 'site_user_ids' in vals:
            for config in self:
                old_users = old_user_sets.get(config.id, set())
                new_users = set(config.site_user_ids.ids)
                added_ids = new_users - old_users
                removed_ids = old_users - new_users
                if added_ids:
                    config._assign_users(self.env['res.users'].browse(list(added_ids)))
                if removed_ids:
                    config._unassign_users(self.env['res.users'].browse(list(removed_ids)))
        return res

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        for record in records:
            if record.site_user_ids:
                record._assign_users(record.site_user_ids)
        return records

    def _assign_users(self, users):
        """
        When users are added to this site config:
        1. Add them to the Site Store security group
        2. Set their default analytic account = this project's analytic account
        3. Set their default warehouse = this project's warehouse
        4. Set their x_site_config_id = this config
        """
        site_store_group = self.env.ref(
            'purchase_demand_raise.group_site_store', raise_if_not_found=False
        )
        for user in users:
            vals = {
                'x_default_analytic_account_id': self.analytic_account_id.id,
                'x_default_warehouse_id': self.warehouse_id.id if self.warehouse_id else False,
                'x_site_config_id': self.id,
            }
            if site_store_group:
                vals['group_ids'] = [(4, site_store_group.id)]
            user.write(vals)

    def _unassign_users(self, users):
        """
        When users are removed from this site config:
        1. Check if they belong to another site config
        2. If not -> remove from Site Store group and clear analytic account
        3. If yes -> update to point to the other config instead
        """
        site_store_group = self.env.ref(
            'purchase_demand_raise.group_site_store', raise_if_not_found=False
        )
        for user in users:
            # Check if the user is assigned to another site config
            other_config = self.search([
                ('site_user_ids', 'in', user.id),
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
                if site_store_group:
                    unwrite_vals['group_ids'] = [(3, site_store_group.id)]
                user.write(unwrite_vals)

    def action_view_users(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Site Users \u2014 %s') % self.name,
            'res_model': 'res.users',
            'view_mode': 'list,form',
            'domain': [('x_site_config_id', '=', self.id)],
            'context': {'default_x_site_config_id': self.id},
        }
