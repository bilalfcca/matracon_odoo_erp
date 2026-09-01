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
from odoo.osv import expression


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

    x_allow_posting = fields.Boolean(
        string='Allow Posting',
        default=True,
        help='When unchecked this account acts as a grouping/header account.\n'
             'It will NOT appear in any account picker dropdown (journal entries,\n'
             'vendor bills, petty cash, etc.) and cannot be selected by users.\n'
             'It remains visible in the Chart of Accounts management screen.\n'
             'Tip: uncheck for main/parent accounts like "101000 Current Assets"\n'
             'that are only used for reporting grouping, not direct posting.',
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
        """Compute the GL balance for this account scoped to the current user's site.

        Two analytic-linking paths are checked (OR):
          1. move header  x_project_analytic_account_id — set on vendor bills (in_invoice)
             and on move entries created by site accountants (via model default).
          2. move-line    analytic_distribution JSONB key — set on MISC journal entries
             made by Head Office and on petty-cash / salary journal entries.

        Using raw SQL because the ORM domain language cannot express the JSONB
        key-exists operator (?).
        """
        analytic_id = self.env.user.x_default_analytic_account_id.id
        if analytic_id and self.ids:
            self.env.cr.execute("""
                SELECT  aml.account_id,
                        COALESCE(SUM(aml.balance), 0) AS balance
                FROM    account_move_line  aml
                JOIN    account_move       am   ON am.id = aml.move_id
                WHERE   aml.account_id = ANY(%s)
                  AND   am.state       = 'posted'
                  AND   (
                            am.x_project_analytic_account_id = %s
                         OR aml.analytic_distribution ? %s
                        )
                GROUP BY aml.account_id
            """, (self.ids, analytic_id, str(analytic_id)))
            by_account = {row[0]: row[1] for row in self.env.cr.fetchall()}
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

        Checks BOTH analytic-linking paths (OR) to be consistent with
        _compute_x_site_balance:
          1. move header  x_project_analytic_account_id
          2. move-line    analytic_distribution JSONB key (HO MISC entries)
        """
        self.ensure_one()
        analytic_id = self.env.user.x_default_analytic_account_id.id
        if analytic_id:
            self.env.cr.execute("""
                SELECT  aml.id
                FROM    account_move_line  aml
                JOIN    account_move       am   ON am.id = aml.move_id
                WHERE   aml.account_id = %s
                  AND   am.state       = 'posted'
                  AND   (
                            am.x_project_analytic_account_id = %s
                         OR aml.analytic_distribution ? %s
                        )
            """, (self.id, analytic_id, str(analytic_id)))
            line_ids = [row[0] for row in self.env.cr.fetchall()]
            domain = [('id', 'in', line_ids)]
        else:
            domain = [('account_id', '=', self.id)]
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

    def name_search(self, name='', domain=None, operator='ilike', limit=100):
        """Exclude non-posting (header/view) accounts from all Many2one pickers.

        Accounts with x_allow_posting = False are purely for COA grouping and must
        never appear in dropdown selectors — journal entries, vendor bills, petty
        cash, batch payments, etc.  The COA management list is not affected because
        it loads records via search() / ORM, not via name_search.

        The filter is bypassed when context key 'show_all_accounts' is True, in
        case any admin screen needs the full list inside a Many2one widget.
        """
        domain = list(domain or [])
        if not self.env.context.get('show_all_accounts'):
            # '!= False' matches True AND NULL (unset), excludes only explicit False.
            # This means all existing accounts (NULL) remain visible; only accounts
            # explicitly unchecked ("Allow Posting" = False) are hidden from pickers.
            domain = [('x_allow_posting', '!=', False)] + domain
        return super().name_search(name=name, domain=domain, operator=operator, limit=limit)

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

    @api.model
    def _search_display_name(self, operator, value):
        """Extend account name search to include children of matched main accounts.

        Odoo's standard _search_display_name matches only against the account's
        own code and name fields.  This extension adds a second pass: when the
        search term matches a *main* account (identified by its code ending in one
        or more trailing zeros, e.g. "101000 Current Assets"), all accounts that
        share the same meaningful code prefix are also returned.

        Example:
            Search "Current Assets" → finds 101000 directly (standard match)
            Extension → detects prefix "101", returns 101300, 101401-406, 101701

        The prefix is derived by stripping trailing zeros:
            101000 → "101"   (3 chars, fine)
            230000 → "23"    (2 chars, fine)
            400000 → "4"     (1 char, skipped — too broad)

        Prefixes shorter than 2 characters are skipped to avoid returning every
        income or expense account when a top-level category is matched.
        """
        base_result = super()._search_display_name(operator, value)

        # Only extend for positive string similarity operators
        if operator not in ('ilike', 'like', '=ilike', '=like', '=') \
                or not isinstance(value, str) or not value:
            return base_result

        # company_root_id is the JSON key used in code_store — e.g. "1" for
        # the root/single company.  str(int_id) converts it to match the key.
        company_root_id = str(self.env.company.root_id.id)

        # Step 1 — find codes of main accounts (ending in at least one '0')
        # whose name matches the search term in ANY installed language.
        self.env.cr.execute("""
            SELECT DISTINCT code_store->>%s AS code
            FROM   account_account
            WHERE  active = TRUE
              AND  code_store->>%s LIKE '%%0'
              AND  EXISTS (
                       SELECT 1 FROM jsonb_each_text(name) jt
                       WHERE  jt.value ILIKE %s
                   )
        """, [company_root_id, company_root_id, f'%{value}%'])

        child_ids = []
        for (code,) in self.env.cr.fetchall():
            if not code:
                continue
            prefix = code.rstrip('0')          # '101000' → '101', '230000' → '23'
            if len(prefix) < 2:
                continue                        # Skip over-broad prefixes like '4'

            # Step 2 — collect posting accounts sharing this prefix
            # x_allow_posting = TRUE excludes header/view accounts from the result
            self.env.cr.execute("""
                SELECT id FROM account_account
                WHERE  active = TRUE
                  AND  COALESCE(x_allow_posting, TRUE) = TRUE
                  AND  code_store->>%s LIKE %s
            """, [company_root_id, prefix + '%'])
            child_ids.extend(row[0] for row in self.env.cr.fetchall())

        if child_ids:
            return expression.OR([base_result, [('id', 'in', child_ids)]])

        return base_result
