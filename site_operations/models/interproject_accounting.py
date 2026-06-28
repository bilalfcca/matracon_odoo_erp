"""Inter-project receivable / payable journal helpers on account.payment.

Each project has an internal res.partner (its accounting identity).
When project A funds project B's vendor payment:

  DR  13100  Inter-Project Receivables    (partner = B)   analytic = A
  CR  21100  Inter-Project Payables       (partner = A)   analytic = B

Reading the partner ledger:
  • Filter by partner B  →  A sees a receivable *from* B  (B owes A)
  • Filter by partner A  →  B sees a payable  *to*   A  (B owes A)

Accounts use asset_receivable / liability_payable so they appear in the
standard partner ledger.  Standard vendor / client AR-AP is NOT mixed with
inter-project balances because the project partners are flagged as
x_is_project_entity = True and carry no real invoicing rank.
"""

from odoo import models, fields, _


class AccountPaymentInterproject(models.Model):
    _inherit = 'account.payment'

    x_interproject_move_ids = fields.Many2many(
        'account.move', 'payment_interproject_move_rel',
        'payment_id', 'move_id',
        string='Inter-Project Entries', readonly=True, copy=False)

    # ─────────────────────────────────────────────────────────────────────────
    # HELPERS
    # ─────────────────────────────────────────────────────────────────────────

    def _get_or_create_interproject_journal(self):
        Journal = self.env['account.journal'].sudo()
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

    def _get_or_create_interproject_account(self, account_type):
        """Inter-project accounts are asset_receivable / liability_payable
        so that counterpart-project entries appear in the partner ledger.

        Account 13100 — Inter-Project Receivables  (asset_receivable)
        Account 21100 — Inter-Project Payables     (liability_payable)
        """
        Account = self.env['account.account'].sudo()
        if account_type == 'receivable':
            account = Account.search(
                [('name', 'ilike', 'Inter-Project Receivable')], limit=1)
            if not account:
                account = Account.create({
                    'name': 'Inter-Project Receivables',
                    'code': '13100',
                    'account_type': 'asset_receivable',
                    'reconcile': True,
                })
            elif account.account_type != 'asset_receivable':
                account.write({'account_type': 'asset_receivable', 'reconcile': True})
        else:
            account = Account.search(
                [('name', 'ilike', 'Inter-Project Payable')], limit=1)
            if not account:
                account = Account.create({
                    'name': 'Inter-Project Payables',
                    'code': '21100',
                    'account_type': 'liability_payable',
                    'reconcile': True,
                })
            elif account.account_type != 'liability_payable':
                account.write({'account_type': 'liability_payable', 'reconcile': True})
        return account

    def _get_project_partner(self, analytic):
        """Return (or lazily create) the internal partner for a project analytic."""
        return analytic.sudo()._get_or_create_internal_partner()

    # ─────────────────────────────────────────────────────────────────────────
    # CORE ENTRY BUILDER
    # ─────────────────────────────────────────────────────────────────────────

    def _create_interproject_entry(self, source_analytic, dest_analytic, amount, ref):
        """Post a balanced inter-project GL entry.

        Source project (the one that GAVE money):
          DR 13100  partner = dest_project   analytic = source
          → Source shows a receivable *from* dest in its partner ledger

        Destination project (the one that RECEIVED money):
          CR 21100  partner = source_project  analytic = dest
          → Dest shows a payable *to* source in its partner ledger
        """
        if not source_analytic or not dest_analytic or amount <= 0:
            return self.env['account.move']
        if source_analytic == dest_analytic:
            return self.env['account.move']

        receivable_account = self._get_or_create_interproject_account('receivable')
        payable_account = self._get_or_create_interproject_account('payable')
        journal = self._get_or_create_interproject_journal()

        # Internal partners — the accounting identities of each project
        source_partner = self._get_project_partner(source_analytic)
        dest_partner = self._get_project_partner(dest_analytic)

        aml_vals = [
            # Source records: "I am owed by [dest]"
            {
                'account_id': receivable_account.id,
                'partner_id': dest_partner.id,
                'name': _('Due from %s (funded %s vendor payment)') % (
                    dest_analytic.name, dest_analytic.name),
                'debit': amount,
                'credit': 0.0,
                'analytic_distribution': {str(source_analytic.id): 100},
            },
            # Dest records: "I owe [source]"
            {
                'account_id': payable_account.id,
                'partner_id': source_partner.id,
                'name': _('Due to %s (received vendor payment funding)') % (
                    source_analytic.name),
                'debit': 0.0,
                'credit': amount,
                'analytic_distribution': {str(dest_analytic.id): 100},
            },
        ]
        move = self.env['account.move'].sudo().create({
            'move_type': 'entry',
            'journal_id': journal.id,
            'ref': ref,
            'date': fields.Date.context_today(self),
            'line_ids': [(0, 0, v) for v in aml_vals],
        })
        move.action_post()

        # Record in the inter-project transfer register
        self.env['x.interproject.transfer'].sudo().create({
            'date': fields.Date.context_today(self),
            'payment_id': self.id,
            'move_id': move.id,
            'source_analytic_id': source_analytic.id,
            'dest_analytic_id': dest_analytic.id,
            'amount': amount,
        })

        return move
