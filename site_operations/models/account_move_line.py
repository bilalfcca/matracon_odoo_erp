from odoo import models, api, _
from odoo.exceptions import UserError


class AccountMoveLineSiteOps(models.Model):
    """Override the payable/receivable constraint for customer invoices.

    In Matracon's workflow customer invoices do not carry due dates — the field
    is intentionally hidden in the UI.  Odoo's core
    ``_check_payable_receivable`` constraint raises:

        "Any journal item on a receivable account must have a due date …"

    when the payment_term line on a customer invoice is not mapped to an
    ``asset_receivable`` account (which can happen when the invoice journal is
    not configured with a dedicated receivable account).

    We suppress that specific XOR check for ``out_invoice`` / ``out_refund``
    documents while preserving ALL other payable/receivable rules:
      •  payable accounts are still forbidden on sale documents.
      •  the full constraint continues to run unchanged for vendor bills,
         journal entries, and every other move type.
    """

    _inherit = 'account.move.line'

    def _check_payable_receivable(self):
        # Split into customer-invoice lines vs. everything else.
        customer_lines = self.filtered(
            lambda l: l.move_id.move_type in ('out_invoice', 'out_refund')
        )
        other_lines = self - customer_lines

        # Run the unmodified core constraint for all non-customer-invoice lines.
        if other_lines:
            super(AccountMoveLineSiteOps, other_lines)._check_payable_receivable()

        # For customer invoices we still enforce: no payable account on a sale doc.
        for line in customer_lines:
            if line.account_id.account_type == 'liability_payable':
                raise UserError(
                    _("Account %s is of payable type, but is used in a sale operation.")
                    % line.account_id.code
                )
        # The 'payment_term XOR asset_receivable' XOR check is intentionally
        # skipped for customer invoices — due dates are not used in this workflow.
