from odoo import models, fields, api


class PaymentProjectAllocation(models.Model):
    _name = 'x.payment.project.allocation'
    _description = 'Payment Fund Allocation by Project'

    payment_id = fields.Many2one(
        'account.payment', ondelete='cascade', required=True)

    project_analytic_account_id = fields.Many2one(
        'account.analytic.account', string='Source Project', required=True)

    allocation_amount = fields.Float(string='Allocation Amount')

    available_balance = fields.Float(
        string='Available Balance',
        compute='_compute_available_balance', store=False)

    # ─────────────────────────────────────────────────────────────────────────
    # COMPUTE
    # ─────────────────────────────────────────────────────────────────────────

    @api.depends('project_analytic_account_id')
    def _compute_available_balance(self):
        for alloc in self:
            project = alloc.project_analytic_account_id
            if not project:
                alloc.available_balance = 0.0
                continue
            # Try to find a site config to get warehouse/journal
            site_config = self.env['x.project.site.config'].search([
                ('analytic_account_id', '=', project.id)
            ], limit=1)
            if site_config and site_config.warehouse_id:
                # Find bank journals associated with this warehouse's company
                # and aggregate analytic-distributed balances
                domain = [
                    ('analytic_distribution', '!=', False),
                    ('parent_state', '=', 'posted'),
                ]
                lines = self.env['account.move.line'].search(domain)
                balance = 0.0
                str_project_id = str(project.id)
                for line in lines:
                    dist = line.analytic_distribution or {}
                    if str_project_id in dist:
                        pct = dist[str_project_id] / 100.0
                        balance += (line.debit - line.credit) * pct
                alloc.available_balance = balance
            else:
                alloc.available_balance = 0.0
