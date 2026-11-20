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

    def _matracon_site_code(self):
        """Return the site warehouse code (e.g. 'MCH', 'RWASA', 'STP') for this
        analytic account, or 'HO' if not linked to any site project config."""
        self.ensure_one()
        if not self.id:
            return 'HO'
        config = self.env['x.project.site.config'].sudo().search(
            [('analytic_account_id', '=', self.id)], limit=1)
        if config and config.warehouse_id and config.warehouse_id.code:
            return config.warehouse_id.code.strip().upper()
        # Fallback: analytic code if set (e.g. 'MCH')
        return (self.code or '').strip().upper() or 'HO'

    @api.model
    def _matracon_site_code_for_id(self, analytic_id):
        """Class-level helper: return the site code for an analytic account ID.
        Returns 'HO' when analytic_id is falsy or no site config is found.
        """
        if not analytic_id:
            return 'HO'
        return self.browse(analytic_id)._matracon_site_code()

    @api.model
    def _matracon_ref_with_site(self, seq_code, site_code):
        """Generate a sequence reference with the site code injected as the
        second path component, keeping all existing separators intact.

        Examples:
          seq  'PCR/00001'       + site 'MCH'  → 'PCR/MCH/00001'
          seq  'EBC/2026/00001'  + site 'MCH'  → 'EBC/MCH/2026/00001'
          seq  'TN-2026-0001'    + site 'HO'   → 'TN-HO-2026-0001'
        """
        seq_val = self.env['ir.sequence'].next_by_code(seq_code)
        if not seq_val:
            return '/'
        if '/' in seq_val:
            idx = seq_val.index('/')
            return seq_val[:idx] + '/' + site_code + '/' + seq_val[idx + 1:]
        if '-' in seq_val:
            idx = seq_val.index('-')
            return seq_val[:idx] + '-' + site_code + '-' + seq_val[idx + 1:]
        return site_code + '/' + seq_val

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        # Auto-create internal partner for project-type analytic accounts.
        # We skip accounts that are clearly not projects (no code, system accounts).
        for rec in records.filtered(lambda r: r.code):
            rec._get_or_create_internal_partner()
        return records
