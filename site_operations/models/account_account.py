"""Project-scoped visibility for Chart of Accounts.

Each account.account record can be tagged with one or more analytic accounts
(sites/projects) via x_site_ids, and can be flagged as Head-Office-Only via
x_ho_only.

Visibility rules (enforced via CoA server action domain; no ir.rule on
account.account since that would break account picker access in bills):
- Head Office / Finance HO always sees ALL accounts regardless of settings.
- Site Accountants see accounts where:
    x_ho_only = False  AND  (x_site_ids is empty  OR  user's analytic ∈ x_site_ids)
- When a Site Accountant creates a new account it is auto-tagged with their
  project so they (and HO) can immediately find and use it.
"""
from odoo import models, fields, api


class AccountJournalSiteOps(models.Model):
    _inherit = 'account.journal'

    x_site_ids = fields.Many2many(
        'account.analytic.account',
        'x_account_journal_site_rel',
        'journal_id', 'analytic_id',
        string='Visible to Sites',
        help='Sites whose accountants can select this journal.\n'
             'Leave empty = visible to all sites.\n'
             'Add specific sites to restrict to Head Office + those sites only.',
    )


class AccountAccountSiteOps(models.Model):
    _inherit = 'account.account'

    x_site_ids = fields.Many2many(
        'account.analytic.account',
        'x_account_account_site_rel',
        'account_id', 'analytic_id',
        string='Visible to Sites',
        help='Projects / sites whose accountants can see and use this account.\n'
             'Leave empty = visible to ALL sites and Head Office (no restriction).\n'
             'Add specific sites to restrict visibility to Head Office + those sites only.\n'
             'Head Office users always see all accounts regardless of this setting.\n'
             'Has no effect when "Head Office Only" is checked.',
    )

    x_ho_only = fields.Boolean(
        string='Head Office Only',
        default=False,
        help='Tick to reserve this account for Head Office use only.\n'
             'When checked, no site accountant can see or select this account '
             'in their Chart of Accounts — even if their site is listed in '
             '"Visible to Sites".\n'
             'Head Office and Finance HO users always see all accounts.',
    )

    # ─── Site-scoped balance ─────────────────────────────────────────────────
    # For site accountants: sum of posted move lines on their project only.
    # For HO / admin: falls back to the standard global balance field.
    x_site_balance = fields.Monetary(
        string='Site Balance',
        compute='_compute_x_site_balance',
        currency_field='currency_id',
    )

    # Flag so view can conditionally show/hide the HO vs site balance button
    # without relying on group XML attributes (groups cannot express "has A but
    # not B").  Marked depends_context so the cache invalidates per user.
    x_user_is_site_accountant = fields.Boolean(
        string='User is Site Accountant',
        compute='_compute_x_user_flags',
    )

    @api.depends_context('uid')
    def _compute_x_user_flags(self):
        is_sa = self.env.user.has_group('site_operations.group_site_accountant')
        for record in self:
            record.x_user_is_site_accountant = is_sa

    @api.depends_context('uid')
    def _compute_x_site_balance(self):
        analytic_id = self.env.user.x_default_analytic_account_id.id
        if analytic_id:
            # Single aggregated query for all accounts in the recordset
            data = self.env['account.move.line'].read_group(
                domain=[
                    ('account_id', 'in', self.ids),
                    ('move_id.x_project_analytic_account_id', '=', analytic_id),
                    ('move_id.state', '=', 'posted'),
                ],
                fields=['account_id', 'balance:sum'],
                groupby=['account_id'],
            )
            by_account = {d['account_id'][0]: d['balance'] for d in data}
            for account in self:
                account.x_site_balance = by_account.get(account.id, 0.0)
        else:
            # HO / admin: x_user_is_site_accountant is False so the Site Balance
            # button is hidden — no need to compute a meaningful value here.
            for account in self:
                account.x_site_balance = 0.0

    def action_open_site_journal_items(self):
        """Open journal items filtered to the current user's site project.
        Called from the 'Site Balance' stat button on the account.account form.
        The ir.rule on account.move.line provides the server-side enforcement;
        the explicit domain here gives the right result even in edge cases."""
        self.ensure_one()
        analytic_id = self.env.user.x_default_analytic_account_id.id
        domain = [('account_id', '=', self.id)]
        if analytic_id:
            domain += [
                ('move_id.x_project_analytic_account_id', '=', analytic_id),
            ]
        return {
            'type': 'ir.actions.act_window',
            'name': 'Journal Items',
            'res_model': 'account.move.line',
            'view_mode': 'list,form',
            'domain': domain,
            'context': {
                'default_account_id': self.id,
                'search_default_posted': 1,
            },
        }

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
