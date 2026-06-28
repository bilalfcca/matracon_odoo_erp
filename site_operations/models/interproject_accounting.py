"""Inter-project receivable / payable journal helpers on account.payment."""

from odoo import models, fields, _


class AccountPaymentInterproject(models.Model):
    _inherit = 'account.payment'

    x_interproject_move_ids = fields.Many2many(
        'account.move', 'payment_interproject_move_rel',
        'payment_id', 'move_id',
        string='Inter-Project Entries', readonly=True, copy=False)

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
        """Inter-project accounts use current-asset / current-liability types.

        We intentionally avoid asset_receivable / liability_payable so these
        internal funding transfers do NOT pollute the aged AR/AP reports.
        Real vendor payables and client receivables should not be mixed with
        inter-project balances. Analytic distribution per project tracks the
        individual balances on each project's dashboard.
        """
        Account = self.env['account.account'].sudo()
        if account_type == 'receivable':
            account = Account.search(
                [('name', 'ilike', 'Inter-Project Receivable')], limit=1)
            if not account:
                account = Account.create({
                    'name': 'Inter-Project Receivables',
                    'code': '13100',
                    'account_type': 'asset_current',
                    'reconcile': False,
                })
            elif account.account_type != 'asset_current':
                # Migrate existing account — was wrongly set as asset_receivable
                account.write({'account_type': 'asset_current', 'reconcile': False})
        else:
            account = Account.search(
                [('name', 'ilike', 'Inter-Project Payable')], limit=1)
            if not account:
                account = Account.create({
                    'name': 'Inter-Project Payables',
                    'code': '21100',
                    'account_type': 'liability_current',
                    'reconcile': False,
                })
            elif account.account_type != 'liability_current':
                # Migrate existing account — was wrongly set as liability_payable
                account.write({'account_type': 'liability_current', 'reconcile': False})
        return account

    def _create_interproject_entry(self, source_analytic, dest_analytic, amount, ref):
        """Source project receivable from destination; dest owes source."""
        if not source_analytic or not dest_analytic or amount <= 0:
            return self.env['account.move']
        if source_analytic == dest_analytic:
            return self.env['account.move']

        receivable_account = self._get_or_create_interproject_account('receivable')
        payable_account = self._get_or_create_interproject_account('payable')
        journal = self._get_or_create_interproject_journal()

        aml_vals = [
            {
                'account_id': receivable_account.id,
                'name': _('Inter-project receivable: %s from %s') % (
                    source_analytic.name, dest_analytic.name),
                'debit': amount,
                'credit': 0.0,
                'analytic_distribution': {str(source_analytic.id): 100},
            },
            {
                'account_id': payable_account.id,
                'name': _('Inter-project payable: %s to %s') % (
                    dest_analytic.name, source_analytic.name),
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

        # Record in the inter-project transfer register so FO can track
        # who owes whom and mark balances as settled when reimbursed.
        self.env['x.interproject.transfer'].sudo().create({
            'date': fields.Date.context_today(self),
            'payment_id': self.id,
            'move_id': move.id,
            'source_analytic_id': source_analytic.id,
            'dest_analytic_id': dest_analytic.id,
            'amount': amount,
        })

        return move
