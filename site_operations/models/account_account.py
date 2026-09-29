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

CoA Hierarchy (x_parent_account_id / x_is_main)
──────────────────────────────────────────────────────────────────────────────
Accounts can be arranged in a parent → child hierarchy:
- x_is_main = True  →  grouping/header account (posting blocked by constraint)
- x_parent_account_id  →  Many2one link to the parent (must be x_is_main=True)
- x_child_account_ids  →  inverse One2many (read-only, computed by Odoo)

Hierarchy sort (x_sort_sequence)
──────────────────────────────────────────────────────────────────────────────
Every account has a stored x_sort_sequence that encodes the FULL parent path:

    grandparent_code_padded / parent_code_padded / own_code_padded

String-sorting x_sort_sequence gives "depth-first parent → child → sibling"
ordering, which is what all financial reports need.  Because _order is
overridden to lead with x_sort_sequence, every report that calls
.sorted() — Trial Balance, General Ledger, Balance Sheet, P&L, Partner
Ledger — automatically honours the custom CoA hierarchy without any extra
per-report code.
"""
from odoo import models, fields, api
from odoo.fields import Domain


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

    # ─── Recursive Group Balance (sum of all descendants) ───────────────────
    x_recursive_balance = fields.Monetary(
        string='Group Balance',
        compute='_compute_x_recursive_balance',
        currency_field='currency_id',
        help='Sum of posted journal entry balances across this account AND all '
             'its child/grandchild accounts recursively.\n'
             'Only meaningful for Main (grouping) accounts.',
    )

    def _compute_x_recursive_balance(self):
        """Recursively sum posted GL balances for this account and all descendants.

        Uses a PostgreSQL recursive CTE to walk the x_parent_account_id tree
        in a single query per call, avoiding N+1 problems.
        """
        for record in self:
            if not record.x_is_main:
                record.x_recursive_balance = 0.0
                continue
            self.env.cr.execute("""
                WITH RECURSIVE account_tree AS (
                    SELECT id FROM account_account WHERE id = %s
                    UNION ALL
                    SELECT aa.id
                    FROM   account_account aa
                    JOIN   account_tree    at ON aa.x_parent_account_id = at.id
                )
                SELECT COALESCE(SUM(aml.balance), 0)
                FROM   account_move_line aml
                JOIN   account_move       am  ON am.id  = aml.move_id
                WHERE  aml.account_id IN (SELECT id FROM account_tree)
                AND    am.state = 'posted'
            """, (record.id,))
            record.x_recursive_balance = self.env.cr.fetchone()[0]

    def action_open_group_journal_items(self):
        """Open journal items for this Main account AND all its descendants.

        Uses the same recursive CTE as _compute_x_recursive_balance so the
        drill-down matches the displayed Group Balance exactly.
        """
        self.ensure_one()
        self.env.cr.execute("""
            WITH RECURSIVE account_tree AS (
                SELECT id FROM account_account WHERE id = %s
                UNION ALL
                SELECT aa.id
                FROM   account_account aa
                JOIN   account_tree    at ON aa.x_parent_account_id = at.id
            )
            SELECT id FROM account_tree
        """, (self.id,))
        account_ids = [row[0] for row in self.env.cr.fetchall()]
        return {
            'type': 'ir.actions.act_window',
            'name': 'Journal Items — %s (Group)' % self.name,
            'res_model': 'account.move.line',
            'view_mode': 'list,form',
            'domain': [
                ('account_id', 'in', account_ids),
                ('move_id.state', '=', 'posted'),
            ],
            'context': {'search_default_posted': 1},
        }

    # ─── Parent / Child Account hierarchy ────────────────────────────────────
    x_parent_account_id = fields.Many2one(
        'account.account',
        string='Parent Account',
        domain=[('x_is_main', '=', True)],
        ondelete='set null',
        help='The Main account this account belongs to.\n'
             'Set this to define the hierarchy — e.g. Vehicles → Fixed Assets → Assets.\n'
             'For bank sub-accounts (Useable Balance / Cash Margin / PO) this is '
             'auto-filled to their parent bank account when created by the system.',
    )

    x_child_account_ids = fields.One2many(
        'account.account',
        'x_parent_account_id',
        string='Child Accounts',
        help='All accounts that belong under this Main account.',
    )

    # ─── Main Account (Group) flags ─────────────────────────────────────────
    x_is_main = fields.Boolean(
        string='Main Account (No Posting)',
        default=False,
        help='Mark this account as a Main/Group account.\n'
             'When checked, journal entries CANNOT use this account in debit or credit.\n'
             'Main accounts are for grouping and reporting only — post to child accounts instead.\n'
             'Enable "Allow Posting Override" below for the rare cases where a main account '
             'must accept direct entries.',
    )
    x_allow_posting_to_main = fields.Boolean(
        string='Allow Posting Override',
        default=False,
        help='Override the Main Account restriction and allow journal entries to post directly.\n'
             'Only enable for exceptional accounts where no child account is appropriate.',
    )

    # ─── Hierarchical sort key ───────────────────────────────────────────────
    x_sort_sequence = fields.Char(
        string='Sort Sequence (Hierarchy)',
        compute='_compute_x_sort_sequence',
        store=True,
        recursive=True,       # parent's sequence must be known before child's
        index=True,           # ORDER BY performance
        help='Auto-computed from the parent→child path.\n'
             'Format: grandparent_code_padded/parent_code_padded/own_code_padded\n'
             'Governs account sort order in ALL financial reports.',
    )

    # Override the default _order so that every .sorted() call — inside
    # _expand_groupby for TB/BS/PL/GL and in M2o dropdown results — returns
    # accounts in parent → child → sibling order.
    _order = 'x_sort_sequence, code'

    @api.depends('code', 'x_parent_account_id', 'x_parent_account_id.x_sort_sequence')
    def _compute_x_sort_sequence(self):
        """Build a lexicographically sortable hierarchy path.

        Each level is the account code left-padded to 15 chars with zeros.
        Levels are joined with '/'.

        Example for a 3-level account (code 221100000, parent 220000000,
        grandparent 200000000):
            sort_sequence = "000000200000000/000000220000000/000000221100000"

        Sorting these strings is a depth-first traversal of the account tree —
        exactly the parent → child → sibling order required by all reports.
        """
        PAD = 15
        for record in self:
            code_padded = (record.code or '').zfill(PAD)
            parent = record.x_parent_account_id
            if parent:
                parent_seq = parent.x_sort_sequence or (parent.code or '').zfill(PAD)
                record.x_sort_sequence = parent_seq + '/' + code_padded
            else:
                record.x_sort_sequence = code_padded

    @api.depends('x_parent_account_id', 'x_parent_account_id.name')
    def _compute_display_name(self):
        """Append the immediate parent account name in parentheses for child accounts.

        Standard Odoo shows: "430100000 Payable To Supplier's"
        With this override:  "430100000 Payable To Supplier's (Current Liabilities)"

        Parent (main) accounts are unaffected since they have no x_parent_account_id.
        Pickers never show main accounts (blocked by name_search) so users only see
        child accounts in dropdowns where the context suffix helps navigation.

        Note: depends on x_parent_account_id.name only (not .display_name) to avoid
        a recursive dependency chain warning — parent.name is sufficient.
        """
        super()._compute_display_name()
        for account in self:
            parent = account.x_parent_account_id
            if parent:
                account.display_name = f"{account.display_name} ({parent.name or ''})"

    @api.model
    def name_search(self, name='', domain=None, operator='ilike', limit=100):
        """Exclude header and main accounts from all Many2one pickers.

        Two mechanisms combined:
          1. x_allow_posting = False  →  soft header/view accounts (no constraint)
          2. x_is_main = True         →  hard main/group accounts (DB constraint blocks posting)

        Both types must be invisible in pickers.  The filter is bypassed when
        context key 'show_all_accounts' is True, and the x_is_main filter is
        skipped when the caller already has an explicit x_is_main domain clause
        (e.g. the x_parent_account_id field itself, which shows ONLY main accounts).
        """
        domain = list(domain or [])
        if not self.env.context.get('show_all_accounts'):
            # 1. Exclude non-posting header accounts (x_allow_posting=False).
            #    '!= False' matches True AND NULL so existing accounts (NULL) remain visible.
            domain = [('x_allow_posting', '!=', False)] + domain
            # 2. Exclude main (grouping) accounts unless the caller explicitly filters
            #    on x_is_main (like the x_parent_account_id picker which shows ONLY main).
            has_main_filter = any(
                isinstance(n, (list, tuple)) and len(n) >= 1 and n[0] == 'x_is_main'
                for n in domain
            )
            if not has_main_filter:
                domain += ['|', ('x_is_main', '=', False), ('x_allow_posting_to_main', '=', True)]
        return super().name_search(name=name, domain=domain, operator=operator, limit=limit)

    @api.model
    def _link_site_ops_accounts(self):
        """Pre-link known XML IDs to existing accounts by code.

        Called from account_configuration_data.xml via <function> BEFORE the
        <record> entries that update those accounts.  Running here (in document
        order) means the XML ID → res_id mapping is always in place when Odoo
        processes the subsequent records — so it performs a safe UPDATE instead
        of a colliding CREATE.

        Idempotent: safe to call on every upgrade regardless of module version.
        Works even when the pre-migrate.py script was skipped because the DB
        was already at the target version (e.g. staging restored from a dev
        snapshot).
        """
        accounts_to_link = [
            ('account_wht_payable', '252100'),
            ('account_retention_payable', '211200'),
        ]
        IrModelData = self.env['ir.model.data'].sudo()
        company_root_id = str(self.env.company.root_id.id)

        for xml_id, code in accounts_to_link:
            self.env.cr.execute(
                "SELECT id FROM account_account WHERE code_store->>%s = %s LIMIT 1",
                (company_root_id, code),
            )
            row = self.env.cr.fetchone()
            if not row:
                continue
            account_id = row[0]

            # Remove any stale mapping (wrong res_id or leftover from a prior run)
            IrModelData.search([
                ('module', '=', 'site_operations'),
                ('name', '=', xml_id),
            ]).unlink()
            # Write the correct mapping
            IrModelData.create({
                'name': xml_id,
                'module': 'site_operations',
                'model': 'account.account',
                'res_id': account_id,
                'noupdate': False,
            })

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
            return Domain(base_result) | Domain([('id', 'in', child_ids)])

        return base_result
