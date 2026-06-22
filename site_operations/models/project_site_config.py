from odoo import models, fields, api, _


class ProjectSiteConfigProjectLink(models.Model):
    _inherit = 'x.project.site.config'

    project_id = fields.Many2one(
        'project.project',
        string='Odoo Project',
        readonly=True,
        copy=False,
        help='Linked native project record — financial dashboard and fund balances.',
    )

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        records._ensure_project_record()
        return records

    def write(self, vals):
        res = super().write(vals)
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
        super()._assign_accountants(users)
        for user in users:
            if self.project_id:
                user.sudo().write({'x_default_project_id': self.project_id.id})

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

    def action_view_project_dashboard(self):
        """Open financial overview for this site."""
        self.ensure_one()
        self._ensure_project_record()
        action = self.env.ref(
            'site_operations.action_project_financial_overview').read()[0]
        action['domain'] = [('id', '=', self.project_id.id)]
        return action
