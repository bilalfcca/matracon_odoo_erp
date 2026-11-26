"""Project on journal items — for grouping only (amounts are not touched).

Project = the entry's 'Project (Site)' (x_project_analytic_account_id) or,
when the entry has none (e.g. HO miscellaneous entries), the line's own
analytic distribution. Same two paths the Site Balance already uses.
"""
from odoo import api, fields, models


class AccountMoveLineProject(models.Model):
    _inherit = 'account.move.line'

    x_project_id = fields.Many2one(
        'account.analytic.account', string='Project', index=True,
        compute='_compute_x_project_id', store=True,
        help="The entry's Project (Site), or the line's analytic account when the entry has none.")

    @api.depends('move_id.x_project_analytic_account_id', 'analytic_distribution')
    def _compute_x_project_id(self):
        Analytic = self.env['account.analytic.account']
        for line in self:
            project = line.move_id.x_project_analytic_account_id
            if not project and line.analytic_distribution:
                for key in line.analytic_distribution:
                    ids = [int(t) for t in str(key).split(',') if t.strip().isdigit()]
                    if ids:
                        project = Analytic.browse(ids[0]).exists()
                        if project:
                            break
            line.x_project_id = project
