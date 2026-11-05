from odoo import api, fields, models


class ResCompanySiteOps(models.Model):
    _inherit = 'res.company'

    # Default deduction accounts for vendor payments. Chosen by Finance on the
    # Company form — never hardcoded by code, so renumbering them in the Chart
    # of Accounts (e.g. WHT Payable → 430510000) is never undone by a build.
    x_wht_payable_account_id = fields.Many2one(
        'account.account',
        string='Default WHT Payable Account',
        help='Credited when WHT is deducted on a vendor payment (unless the '
             'vendor\'s WHT exemption names its own account), and debited by '
             'the companion FBR payment.',
    )
    x_retention_payable_account_id = fields.Many2one(
        'account.account',
        string='Default Retention Payable Account',
        help='Credited when retention is deducted on a vendor payment.',
    )

    @api.model
    def _matracon_adopt_deduction_accounts(self):
        """One-time handover from the old site_operations XML records.

        WHT / Retention Payable used to be module data records
        (site_operations.account_wht_payable / account_retention_payable),
        which reset their code on every upgrade. Copy the accounts those
        xmlids point to into the company fields (if not already set), then
        drop the xmlids so the accounts belong to Finance only and are never
        touched — or deleted — by a module upgrade. Idempotent.
        """
        IMD = self.env['ir.model.data'].sudo()
        field_by_xmlid = {
            'account_wht_payable': 'x_wht_payable_account_id',
            'account_retention_payable': 'x_retention_payable_account_id',
        }
        for xml_id, field_name in field_by_xmlid.items():
            imd = IMD.search([
                ('module', '=', 'site_operations'),
                ('name', '=', xml_id),
            ])
            account = self.env['account.account'].sudo().browse(
                imd[:1].res_id).exists()
            if account:
                domain = [(field_name, '=', False)]
                if account.company_ids:
                    domain.append(('id', 'child_of', account.company_ids.ids))
                companies = self.sudo().search(domain)
                companies.write({field_name: account.id})
            imd.unlink()
