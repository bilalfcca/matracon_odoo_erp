"""Shared inter-project receivable / payable journal helpers."""

from odoo import models, fields, _


class InterprojectAccountingMixin(models.AbstractModel):
    _name = 'x.interproject.accounting.mixin'
    _description = 'Inter-Project Accounting Helpers'

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
        return move
