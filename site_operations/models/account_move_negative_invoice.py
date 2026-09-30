"""Allow posting Customer Invoices (out_invoice) with a negative total.

Matracon bills NHA and other clients with negative invoices (deductions /
adjustments) instead of credit notes. Odoo 19 blocks this inside
``account.move._post()`` with one inline check:

    if float_compare(invoice.amount_total, 0.0, precision_rounding=...) < 0:
        validation_msgs.add("You cannot validate an invoice with a negative total amount. ...")

There is no hook or setting for it, and copying ``_post()`` would drift
from Odoo.sh's automatic Odoo updates. Instead, the ``float_compare`` name
inside ``odoo.addons.account.models.account_move`` is wrapped so that this
single comparison reports "not negative" when, and only when:

  * the caller is ``_post`` (the only function in that module comparing a
    total to 0.0 this way),
  * the value compared is exactly that invoice's ``amount_total``,
  * the move is a Customer Invoice (``out_invoice``), and
  * site_operations is loaded in the move's registry.

Everything else - vendor bills, refunds, receipts, every other check in
``_post`` and every other ``float_compare`` call - behaves exactly as in
standard Odoo. If Odoo ever rewrites that check, the wrapper simply stops
matching and the standard error comes back; it cannot post anything Odoo
would otherwise reject for a different reason.
"""
import sys

from odoo.addons.account.models import account_move as account_move_module

_original_float_compare = account_move_module.float_compare


def _float_compare_allow_negative_customer_invoice(value1, value2, precision_digits=None, precision_rounding=None):
    result = _original_float_compare(
        value1, value2, precision_digits=precision_digits, precision_rounding=precision_rounding,
    )
    if result < 0 and value2 == 0.0:
        caller = sys._getframe(1)
        if caller.f_code.co_name == '_post':
            invoice = caller.f_locals.get('invoice')
            if (
                invoice is not None
                and getattr(invoice, '_name', None) == 'account.move'
                and 'x_project_analytic_account_id' in invoice._fields
                and invoice.move_type == 'out_invoice'
                and value1 == invoice.amount_total
            ):
                return 0
    return result


_float_compare_allow_negative_customer_invoice._matracon_allow_negative = True

if not getattr(account_move_module.float_compare, '_matracon_allow_negative', False):
    account_move_module.float_compare = _float_compare_allow_negative_customer_invoice
