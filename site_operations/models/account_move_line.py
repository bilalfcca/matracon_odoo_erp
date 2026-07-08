from odoo import models, api, _
from odoo.exceptions import UserError


class AccountMoveLineSiteOps(models.Model):
    """Customisations to account.move.line for Matracon's workflow.

    1. Constraint override — customer invoices
       ----------------------------------------
       In Matracon's workflow customer invoices do not carry due dates — the
       field is intentionally hidden in the UI.  Odoo's core
       ``_check_payable_receivable`` constraint raises:

           "Any journal item on a receivable account must have a due date …"

       We suppress that specific XOR check for ``out_invoice`` / ``out_refund``
       documents while preserving ALL other payable/receivable rules.

    2. Real-time balancing of the last (AP/AR) row
       -----------------------------------------------
       The Journal Entries tab shows the full journal including the auto-managed
       Account Payable / Account Receivable (payment_term) line at the bottom.
       Odoo only rebuilds that line on Save (_sync_dynamic_lines inside write).

       By adding ``@api.onchange('debit', 'credit')`` on the LINE ITSELF we
       update the payment_term balance *immediately* when the user edits any
       other row — no Save required.  This is more reliable than a parent
       ``@api.onchange('line_ids')`` which only fires on row add/remove, not
       when fields within existing rows change.
    """

    _inherit = 'account.move.line'

    # ── 1. Constraint override ────────────────────────────────────────────────

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

    # ── 2. Real-time last-row balance ─────────────────────────────────────────

    @api.onchange('debit', 'credit')
    def _onchange_amount_rebalance_last_row(self):
        """When the user edits debit or credit on any journal line, immediately
        recompute the payment_term (Account Payable / Account Receivable) line
        so that the last row always shows the correct running balance.

        This fires on the line being edited — which is why it updates in
        real-time (field-by-field) rather than waiting for Save.

        The calculation is simply:
            term_balance = sum(credit of other lines) − sum(debit of other lines)
        A positive result means debit side (receivable on customer invoice).
        A negative result means credit side (payable on vendor bill).
        """
        # Don't recurse if someone edits the payment_term line itself.
        if self.display_type == 'payment_term':
            return
        move = self.move_id
        if not move or not move.is_invoice(include_receipts=True) or move.state != 'draft':
            return
        term_lines = move.line_ids.filtered(lambda l: l.display_type == 'payment_term')
        if len(term_lines) != 1:
            return   # complex payment-term schedule: leave Odoo to handle on Save
        other_lines = move.line_ids.filtered(lambda l: l.display_type != 'payment_term')
        total_debit  = sum(l.debit  or 0.0 for l in other_lines)
        total_credit = sum(l.credit or 0.0 for l in other_lines)
        term_lines[0].balance = total_credit - total_debit
