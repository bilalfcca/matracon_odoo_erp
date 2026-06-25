from odoo import models, fields, api, _

# Demo / default site warehouses — one per Matracon project.
_SITE_WAREHOUSE_DEFAULTS = {
    'MCH - BAHAWALNAGAR': ('MCH', 'MCH Site Warehouse', 'warehouse_site_mch'),
    'RWASA': ('RWASA', 'RWASA Site Warehouse', 'warehouse_site_rwasa'),
    'STP - MARDAN': ('STP', 'STP Site Warehouse', 'warehouse_site_stp'),
}


class ProjectSiteConfigProjectLink(models.Model):
    _inherit = 'x.project.site.config'

    project_id = fields.Many2one(
        'project.project',
        string='Odoo Project',
        readonly=True,
        copy=False,
        help='Linked native project record — financial dashboard and fund balances.',
    )

    x_site_accountant_ids = fields.Many2many(
        'res.users',
        'x_project_site_accountant_rel',
        'config_id', 'user_id',
        string='Site Accountants',
        help=(
            'Site Accountants for this project. Adding a user here automatically assigns '
            'the Site Accountant security group and sets their default analytic account.'
        ),
    )
    x_accountant_count = fields.Integer(
        compute='_compute_accountant_count',
        string='Accountants',
    )

    @api.depends('x_site_accountant_ids')
    def _compute_accountant_count(self):
        for config in self:
            config.x_accountant_count = len(config.x_site_accountant_ids)

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        records._ensure_project_record()
        for record in records:
            if record.x_site_accountant_ids:
                record._assign_accountants(record.x_site_accountant_ids)
        return records

    def write(self, vals):
        old_accountant_sets = {
            config.id: set(config.x_site_accountant_ids.ids) for config in self
        }
        res = super().write(vals)
        if 'x_site_accountant_ids' in vals:
            for config in self:
                old_users = old_accountant_sets.get(config.id, set())
                new_users = set(config.x_site_accountant_ids.ids)
                added_ids = new_users - old_users
                removed_ids = old_users - new_users
                if added_ids:
                    config._assign_accountants(
                        self.env['res.users'].browse(list(added_ids)))
                if removed_ids:
                    config._unassign_accountants(
                        self.env['res.users'].browse(list(removed_ids)))
        if any(k in vals for k in (
            'name', 'analytic_account_id', 'site_user_ids', 'x_site_accountant_ids',
        )):
            self._ensure_project_record()
        return res

    def _ensure_project_record(self):
        """Create or link project.project for each site configuration."""
        Project = self.env['project.project']
        for config in self:
            if not config.analytic_account_id:
                continue
            project = config.project_id
            if not project:
                project = Project.search(
                    [('x_analytic_account_id', '=', config.analytic_account_id.id)],
                    limit=1,
                )
            if not project:
                project = Project.create({
                    'name': config.name,
                    'x_analytic_account_id': config.analytic_account_id.id,
                })
            else:
                project.write({
                    'name': config.name,
                    'x_analytic_account_id': config.analytic_account_id.id,
                })
            config.project_id = project.id
            project.write({
                'x_site_config_id': config.id,
                'x_site_store_user_ids': [(6, 0, config.site_user_ids.ids)],
                'x_site_accountant_user_ids': [
                    (6, 0, config.x_site_accountant_ids.ids)
                ],
            })

    def _assign_users(self, users):
        super()._assign_users(users)
        for user in users:
            if self.project_id:
                user.sudo().write({'x_default_project_id': self.project_id.id})

    def _assign_accountants(self, users):
        """Assign Site Accountant group and project defaults."""
        accountant_group = self.env.ref(
            'site_operations.group_site_accountant', raise_if_not_found=False)
        Users = self.env['res.users']
        for user in users:
            vals = {
                'x_default_analytic_account_id': self.analytic_account_id.id,
                'x_default_warehouse_id': (
                    self.warehouse_id.id if self.warehouse_id else False),
                'x_site_config_id': self.id,
            }
            user.sudo().write(vals)
            if accountant_group:
                Users._matracon_add_group(user, accountant_group)
            if self.project_id:
                user.sudo().write({'x_default_project_id': self.project_id.id})

    def _unassign_accountants(self, users):
        """Reverse accountant assignment when removed from site config."""
        accountant_group = self.env.ref(
            'site_operations.group_site_accountant', raise_if_not_found=False)
        Users = self.env['res.users']
        for user in users:
            other_config = self.search([
                ('x_site_accountant_ids', 'in', user.id),
                ('id', '!=', self.id),
            ], limit=1)
            if other_config:
                user.sudo().write({
                    'x_default_analytic_account_id': other_config.analytic_account_id.id,
                    'x_default_warehouse_id': (
                        other_config.warehouse_id.id
                        if other_config.warehouse_id else False),
                    'x_site_config_id': other_config.id,
                    'x_default_project_id': (
                        other_config.project_id.id
                        if other_config.project_id else False),
                })
            else:
                unwrite_vals = {
                    'x_default_analytic_account_id': False,
                    'x_default_warehouse_id': False,
                    'x_site_config_id': False,
                    'x_default_project_id': False,
                }
                user.sudo().write(unwrite_vals)
                if accountant_group:
                    Users._matracon_remove_group(user, accountant_group)

    def action_open_project(self):
        self.ensure_one()
        self._ensure_project_record()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Project — %s') % self.name,
            'res_model': 'project.project',
            'view_mode': 'form',
            'res_id': self.project_id.id,
        }

    def action_view_accountants(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Site Accountants — %s') % self.name,
            'res_model': 'res.users',
            'view_mode': 'list,form',
            'domain': [('id', 'in', self.x_site_accountant_ids.ids)],
            'context': {'default_x_site_config_id': self.id},
        }

    def action_view_project_dashboard(self):
        """Open financial overview for this site."""
        self.ensure_one()
        self._ensure_project_record()
        action = self.env.ref(
            'site_operations.action_project_financial_overview').read()[0]
        action['domain'] = [('id', '=', self.project_id.id)]
        return action

    @api.model
    def _matracon_ensure_site_warehouses(self):
        """Ensure each site project config has a warehouse (demo + production)."""
        Warehouse = self.env['stock.warehouse'].sudo()
        company = self.env.company
        for config in self.search([]):
            if config.warehouse_id:
                continue
            wh = False
            defaults = _SITE_WAREHOUSE_DEFAULTS.get(config.name)
            if defaults:
                _code, _name, xml_suffix = defaults
                wh = self.env.ref(
                    f'site_operations.{xml_suffix}', raise_if_not_found=False)
            if not wh and defaults:
                code, name, _xml_suffix = defaults
                wh = Warehouse.search([
                    ('code', '=', code),
                    ('company_id', '=', company.id),
                ], limit=1)
                if not wh:
                    wh = Warehouse.create({
                        'name': name,
                        'code': code,
                        'company_id': company.id,
                    })
            elif not wh:
                code = ''.join(
                    part[0] for part in config.name.split() if part
                )[:5].upper() or 'SITE'
                name = f'{config.name} Site Warehouse'
                wh = Warehouse.search([
                    ('code', '=', code),
                    ('company_id', '=', company.id),
                ], limit=1)
                if not wh:
                    wh = Warehouse.create({
                        'name': name,
                        'code': code,
                        'company_id': company.id,
                    })
            config.sudo().write({'warehouse_id': wh.id})
            users = config.site_user_ids | config.x_site_accountant_ids
            if users:
                users.sudo().write({'x_default_warehouse_id': wh.id})
