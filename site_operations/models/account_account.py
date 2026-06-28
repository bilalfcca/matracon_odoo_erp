"""Project-scoped visibility for Chart of Accounts.

Each account.account record can be tagged with one or more analytic accounts
(sites/projects) via x_site_ids.

Rules:
- Head Office (group_account_manager / group_finance_ho) always sees ALL accounts.
- Site Accountants (group_site_accountant) see only:
    • Accounts whose x_site_ids contains their assigned analytic account
    • Accounts they personally created (so they can work with accounts they add)
- When a Site Accountant creates a new account it is automatically tagged with
  their project so they (and HO) can always find it.
"""
from odoo import models, fields, api


class AccountJournalSiteOps(models.Model):
    _inherit = 'account.journal'

    x_site_ids = fields.Many2many(
        'account.analytic.account',
        'x_account_journal_site_rel',
        'journal_id', 'analytic_id',
        string='Visible to Sites',
        help='Sites whose accountants can select this journal (e.g. for petty cash expenses).\n'
             'Leave empty to restrict to Head Office only.',
    )


class AccountAccountSiteOps(models.Model):
    _inherit = 'account.account'

    x_site_ids = fields.Many2many(
        'account.analytic.account',
        'x_account_account_site_rel',
        'account_id', 'analytic_id',
        string='Visible to Sites',
        help='Projects / sites whose accountants can see and use this account.\n'
             'Leave empty to restrict visibility to Head Office only.\n'
             'Head Office users always see all accounts regardless of this setting.',
    )

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        # Auto-tag with the creating user's site so site accountants can
        # immediately find and use the accounts they create.
        for rec in records:
            analytic = rec.create_uid.sudo().x_default_analytic_account_id
            if analytic and analytic.id not in rec.x_site_ids.ids:
                rec.sudo().x_site_ids = [(4, analytic.id)]
        return records
