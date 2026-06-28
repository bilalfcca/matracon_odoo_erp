"""Each analytic account (project) gets an internal res.partner.

This partner serves as the accounting identity of the project so that
inter-project receivables and payables can be tied to a specific counterpart
and appear in the partner ledger under the correct project name.

The partner is created automatically and is tagged with x_is_project_entity=True
so it can be filtered out of vendor/customer lists.
"""
from odoo import models, fields, api, _


class AccountAnalyticAccountSiteOps(models.Model):
    _inherit = 'account.analytic.account'

    x_internal_partner_id = fields.Many2one(
        'res.partner',
        string='Internal Project Partner',
        copy=False, readonly=True,
        help='Auto-created partner used as the accounting counterpart '
             'for inter-project receivable / payable entries.',
    )

    def _get_or_create_internal_partner(self):
        """Return the internal partner for this analytic account, creating it
        if it does not yet exist."""
        self.ensure_one()
        if self.x_internal_partner_id:
            return self.x_internal_partner_id

        # Build a distinct name: "[CODE] Name" or just "Name"
        display = (
            f'[{self.code}] {self.name}' if self.code else self.name
        ) or _('Project')

        partner = self.env['res.partner'].sudo().create({
            'name': display,
            'company_type': 'company',
            'supplier_rank': 0,
            'customer_rank': 0,
            'x_is_project_entity': True,
            'comment': _('Auto-generated internal partner for project accounting.'),
        })
        self.x_internal_partner_id = partner
        return partner

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        # Auto-create internal partner for project-type analytic accounts.
        # We skip accounts that are clearly not projects (no code, system accounts).
        for rec in records.filtered(lambda r: r.code):
            rec._get_or_create_internal_partner()
        return records
