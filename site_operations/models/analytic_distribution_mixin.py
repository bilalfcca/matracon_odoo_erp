"""Shared helpers for project analytic tagging across Matracon modules."""

from odoo import models, api


class AccountPaymentAnalyticHelpers(models.Model):
    """Analytic helper methods on account.payment (single _inherit — Odoo 19 safe)."""
    _inherit = 'account.payment'

    @api.model
    def _analytic_distribution_for_account(self, analytic_account):
        """Return Odoo 19 analytic_distribution dict for 100% on one account."""
        if not analytic_account:
            return {}
        return {str(analytic_account.id): 100.0}

    @api.model
    def _apply_analytic_to_move_lines(self, move_lines, analytic_account):
        """Tag account.move.line recordset with project analytic distribution."""
        dist = self._analytic_distribution_for_account(analytic_account)
        if dist and move_lines:
            move_lines.write({'analytic_distribution': dist})

    @api.model
    def _resolve_analytic_from_project(self, project):
        """project.project → account.analytic.account."""
        if not project:
            return self.env['account.analytic.account']
        if project.x_analytic_account_id:
            return project.x_analytic_account_id
        return self.env['account.analytic.account']

    @api.model
    def _project_for_analytic(self, analytic_account):
        """account.analytic.account → project.project (if linked)."""
        if not analytic_account:
            return self.env['project.project']
        return self.env['project.project'].search(
            [('x_analytic_account_id', '=', analytic_account.id)], limit=1)
