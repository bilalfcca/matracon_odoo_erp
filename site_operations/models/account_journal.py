from odoo import models, fields, api


class AccountJournalSiteOps(models.Model):
    _inherit = 'account.journal'

    bg_facility_ids = fields.One2many(
        'x.bank.guarantee.facility', 'journal_id', string='Bank Guarantee Facilities'
    )
    x_cash_margin_account_id = fields.Many2one(
        'account.account',
        string='Cash Margin Account',
        domain="[('x_is_main', '=', False), ('deprecated', '=', False)]",
        help='Cash Margin account used for bank guarantees issued through this bank. '
             'If left empty, automatically determined from the bank\'s Cash Margin child account.',
    )

    def _matracon_sync_payment_method_accounts(self):
        """For every bank/cash journal, set payment_account_id on all Manual
        Payment method lines that currently have no outstanding account.

        Odoo creates the method lines automatically but leaves payment_account_id
        blank.  We default it to journal.default_account_id (the bank account
        itself), which is the standard practice for manual cheque workflows where
        no separate outstanding/transit account is used.
        """
        for journal in self.filtered(lambda j: j.type in ('bank', 'cash')):
            account = journal.default_account_id
            if not account:
                continue
            lines = (
                journal.inbound_payment_method_line_ids
                | journal.outbound_payment_method_line_ids
            ).filtered(lambda l: not l.payment_account_id)
            if lines:
                lines.payment_account_id = account

    @api.model_create_multi
    def create(self, vals_list):
        journals = super().create(vals_list)
        journals._matracon_sync_payment_method_accounts()
        return journals

    def write(self, vals):
        res = super().write(vals)
        # Re-sync whenever the type or the bank/default account changes.
        if any(k in vals for k in ('type', 'default_account_id', 'bank_account_id')):
            self._matracon_sync_payment_method_accounts()
        return res
