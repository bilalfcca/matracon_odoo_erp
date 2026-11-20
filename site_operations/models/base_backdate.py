"""
base_backdate.py — Global default-date override for the "Backdate Default" user preference.

When a user sets x_backdate_default on their profile (Change my Preferences →
Default Entry Date), every new record they open will have its transaction-date
field pre-filled with that date instead of today.

One override on the `base` abstract model covers every model without needing
per-model changes. Only fields in _TRANSACTION_DATE_FIELDS are touched —
deadline, period, validity, and filter fields are intentionally excluded.
"""

from odoo import models, fields, api

# Fields that carry the "document / transaction date" of a record.
# Deliberately excludes: date_deadline, date_end, date_from, date_to,
# validity_date, date_maturity — those are deadline / period / filter dates.
_TRANSACTION_DATE_FIELDS = frozenset({
    'date',             # account.move (journal entries), account.payment,
                        # employee/subcontractor backcharges, many others
    'invoice_date',     # account.move (vendor bills, customer invoices)
    'date_order',       # purchase.order (PO / RFQ)
    'scheduled_date',   # stock.picking (material issuance / return / transfer)
    'expense_date',     # x.petty.cash.expense
    'backcharge_date',  # x.employee.backcharge, x.subcontractor.backcharge
    'date_done',        # stock.picking (actual done date, when used manually)
})


class BaseBackdate(models.AbstractModel):
    """Injects the user's backdate preference into transaction-date fields."""
    _inherit = 'base'

    @api.model
    def default_get(self, fields_list):
        vals = super().default_get(fields_list)

        backdate = self.env.user.x_backdate_default
        if not backdate:
            return vals

        for fname in fields_list:
            if fname not in _TRANSACTION_DATE_FIELDS:
                continue
            field = self._fields.get(fname)
            if not field:
                continue
            if field.type == 'date':
                vals[fname] = backdate
            elif field.type == 'datetime':
                # Convert the chosen date to midnight of that day (naive UTC string)
                # so Datetime fields (e.g. scheduled_date on stock.picking) work correctly.
                vals[fname] = fields.Datetime.to_datetime(backdate)

        return vals
