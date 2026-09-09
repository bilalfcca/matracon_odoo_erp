"""Inter-project receivable / payable journal helpers.

Each project has an internal res.partner (its accounting identity).
When project A funds project B's vendor payment or journal entry:

  DR  13100  Inter-Project Receivables    (partner = B)   analytic = A
  CR  21100  Inter-Project Payables       (partner = A)   analytic = B

Reading the partner ledger:
  • Filter by partner B  →  A sees a receivable *from* B  (B owes A)
  • Filter by partner A  →  B sees a payable  *to*   A  (B owes A)

Accounts use asset_receivable / liability_payable so they appear in the
standard partner ledger.  Standard vendor / client AR-AP is NOT mixed with
inter-project balances because the project partners are flagged as
x_is_project_entity = True and carry no real invoicing rank.

Both account.payment and account.move (journal entries) can trigger
inter-project entries — helpers are defined once here and used by both.
"""

from odoo import models, fields, _


# ─────────────────────────────────────────────────────────────────────────────
# SHARED HELPERS — model-independent (use env, not self)
# ─────────────────────────────────────────────────────────────────────────────

def _get_or_create_interproject_journal(env):
    Journal = env['account.journal'].sudo()
    journal = Journal.search([
        ('name', '=', 'Inter-Project Transfers'),
        ('type', '=', 'general'),
    ], limit=1)
    if not journal:
        journal = Journal.create({
            'name': 'Inter-Project Transfers',
            'type': 'general',
            'code': 'IPTR',
        })
    return journal


def _get_or_create_interproject_account(env, account_type):
    """Inter-project accounts: asset_receivable (13100) / liability_payable (21100).

    Search order:
      1. By code — fastest, unique, survives chart-of-account renames.
      2. By name (ilike) — fallback for installs where the code was manually changed.
      3. Create — only when neither lookup succeeds.

    Using code as the primary key avoids a ValidationError on production databases
    that already have an account with the same code but a different name: a
    name-only lookup would find nothing → try to create → Odoo 19's
    _ensure_code_is_unique detects the existing record → raises "Account codes must
    be unique: XXXXX".
    """
    Account = env['account.account'].sudo()
    if account_type == 'receivable':
        code, name, acct_type = '13100', 'Inter-Project Receivables', 'asset_receivable'
    else:
        code, name, acct_type = '21100', 'Inter-Project Payables', 'liability_payable'

    # 1. Look up by code (authoritative — code is unique per company in Odoo 19).
    account = Account.search([('code', '=', code)], limit=1)

    # 2. Fallback: name search for installs where the code may differ.
    if not account:
        account = Account.search([('name', 'ilike', name[:20])], limit=1)

    if account:
        # Ensure correct account_type and reconcile flag regardless of how we found it.
        if account.account_type != acct_type or not account.reconcile:
            account.write({'account_type': acct_type, 'reconcile': True})
        return account

    # 3. Create the account — it doesn't exist yet.
    from odoo.exceptions import ValidationError as OdooValidationError
    try:
        account = Account.create({
            'name': name,
            'code': code,
            'account_type': acct_type,
            'reconcile': True,
        })
    except OdooValidationError:
        # Another concurrent call in the same batch may have just created this
        # account.  Invalidate caches and retry the code lookup.
        Account.invalidate_model(['code', 'code_store'])
        account = Account.search([('code', '=', code)], limit=1)
        if not account:
            raise
    return account


def _get_project_partner(analytic):
    return analytic.sudo()._get_or_create_internal_partner()


def _build_interproject_entry(env, source_analytic, dest_analytic, amount, ref, date=None):
    """Post a balanced inter-project GL entry and return the account.move record.

    Source project (the one that GAVE money):
      DR 13100  partner = dest_project   analytic = source
      → Source shows a receivable *from* dest in its partner ledger

    Destination project (the one that RECEIVED money):
      CR 21100  partner = source_project  analytic = dest
      → Dest shows a payable *to* source in its partner ledger
    """
    if not source_analytic or not dest_analytic or amount <= 0:
        return env['account.move']
    if source_analytic == dest_analytic:
        return env['account.move']

    receivable_account = _get_or_create_interproject_account(env, 'receivable')
    payable_account = _get_or_create_interproject_account(env, 'payable')
    journal = _get_or_create_interproject_journal(env)

    source_partner = _get_project_partner(source_analytic)
    dest_partner = _get_project_partner(dest_analytic)

    entry_date = date or fields.Date.context_today(env['account.move'])

    aml_vals = [
        # Source records: "I am owed by [dest]"
        {
            'account_id': receivable_account.id,
            'partner_id': dest_partner.id,
            'name': _('Due from %s (funded JE payment)') % dest_analytic.name,
            'debit': amount,
            'credit': 0.0,
            'analytic_distribution': {str(source_analytic.id): 100},
        },
        # Dest records: "I owe [source]"
        {
            'account_id': payable_account.id,
            'partner_id': source_partner.id,
            'name': _('Due to %s (received funding)') % source_analytic.name,
            'debit': 0.0,
            'credit': amount,
            'analytic_distribution': {str(dest_analytic.id): 100},
        },
    ]
    move = env['account.move'].sudo().create({
        'move_type': 'entry',
        'journal_id': journal.id,
        'ref': ref,
        'date': entry_date,
        'line_ids': [(0, 0, v) for v in aml_vals],
    })
    move.action_post()
    return move


# ─────────────────────────────────────────────────────────────────────────────
# account.payment — inter-project entries on vendor payments
# ─────────────────────────────────────────────────────────────────────────────

class AccountPaymentInterproject(models.Model):
    _inherit = 'account.payment'

    x_interproject_move_ids = fields.Many2many(
        'account.move', 'payment_interproject_move_rel',
        'payment_id', 'move_id',
        string='Inter-Project Entries', readonly=True, copy=False)

    # Keep instance methods as thin wrappers around the shared helpers
    # so existing call-sites in account_payment.py don't need changes.

    def _get_or_create_interproject_journal(self):
        return _get_or_create_interproject_journal(self.env)

    def _get_or_create_interproject_account(self, account_type):
        return _get_or_create_interproject_account(self.env, account_type)

    def _get_project_partner(self, analytic):
        return _get_project_partner(analytic)

    def _create_interproject_entry(self, source_analytic, dest_analytic, amount, ref):
        """Post a balanced inter-project GL entry and record in transfer register."""
        move = _build_interproject_entry(
            self.env, source_analytic, dest_analytic, amount, ref,
            date=fields.Date.context_today(self),
        )
        if move:
            self.env['x.interproject.transfer'].sudo().create({
                'date': fields.Date.context_today(self),
                'payment_id': self.id,
                'move_id': move.id,
                'source_analytic_id': source_analytic.id,
                'dest_analytic_id': dest_analytic.id,
                'amount': amount,
            })
        return move


# ─────────────────────────────────────────────────────────────────────────────
# account.move — inter-project entries on Finance-HO journal entries
# ─────────────────────────────────────────────────────────────────────────────

class AccountMoveInterproject(models.Model):
    _inherit = 'account.move'

    x_je_allocation_ids = fields.One2many(
        'x.je.project.allocation', 'move_id',
        string='Fund Allocation',
        copy=False,
        help='Source project pools that fund this journal entry. '
             'When the entry is posted:\n'
             '• Each project\'s available balance decreases by its allocation amount.\n'
             '• If source ≠ destination project, an inter-project receivable/payable '
             'entry is automatically created.',
    )

    x_je_interproject_move_ids = fields.Many2many(
        'account.move',
        'je_interproject_move_rel',
        'je_id', 'ipm_id',
        string='Inter-Project Entries',
        readonly=True,
        copy=False,
        help='Auto-created inter-project GL entries (posted when this JE is posted).',
    )

    def _create_je_interproject_entries(self):
        """Create inter-project GL entries for each allocation line where src ≠ dest.

        Called from action_post() after the JE is posted.
        """
        self.ensure_one()
        dest = self.x_ho_dest_project_id
        if not dest or not self.x_je_allocation_ids:
            return

        ref = _('JE %s — inter-project') % (self.name or '')
        moves = self.env['account.move']

        for alloc in self.x_je_allocation_ids.filtered(
            lambda a: a.allocation_amount > 0
        ):
            src = alloc.project_analytic_account_id
            if src and src != dest:
                move = _build_interproject_entry(
                    self.env, src, dest, alloc.allocation_amount, ref,
                    date=self.date or fields.Date.context_today(self),
                )
                if move:
                    moves |= move

        if moves:
            self.x_je_interproject_move_ids = [(6, 0, moves.ids)]
