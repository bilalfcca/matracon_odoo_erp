"""Cheque number of a journal item, for reports (General Ledger).

Read-only lookup over the EXISTING cheque fields; nothing is stored or
generated here. Order of preference (most specific first):

  1. the line's own cheque          (multi-bank splits, JE lines)
  2. the journal entry header       (manual JE / single-bank payment)
  3. the payment the entry belongs to
  4. otherwise, the distinct cheques on the entry's other lines
     (e.g. the vendor line of a multi-bank payment shows all its cheques)

At each level a cheque picked from the series (x_cheque_leaf_id) wins over
the typed text; if no series leaf is picked, the manually typed number
(x_cheque_number) is used.
"""
from odoo import api, models


class AccountMoveLineCheque(models.Model):
    _inherit = 'account.move.line'

    @api.model
    def _mtr_get_cheque_numbers(self, aml_ids):
        """{aml_id: 'cheque no.'} for the given ids (ids without one are omitted)."""
        aml_ids = [i for i in aml_ids if i]
        if not aml_ids:
            return {}
        self.env.flush_all()
        self.env.cr.execute("""
            WITH own AS (
                SELECT aml.id, aml.move_id,
                       COALESCE(NULLIF(TRIM(leaf_l.cheque_number), ''), NULLIF(TRIM(aml.x_cheque_number), '')) AS line_no,
                       COALESCE(NULLIF(TRIM(leaf_m.cheque_number), ''), NULLIF(TRIM(am.x_cheque_number), '')) AS move_no,
                       COALESCE(NULLIF(TRIM(leaf_p.cheque_number), ''), NULLIF(TRIM(pay.x_cheque_number), '')) AS pay_no
                  FROM account_move_line aml
                  JOIN account_move am ON am.id = aml.move_id
             LEFT JOIN x_cheque_leaf leaf_l ON leaf_l.id = aml.x_cheque_leaf_id
             LEFT JOIN x_cheque_leaf leaf_m ON leaf_m.id = am.x_cheque_leaf_id
             LEFT JOIN account_payment pay ON pay.id = am.origin_payment_id
             LEFT JOIN x_cheque_leaf leaf_p ON leaf_p.id = pay.x_cheque_leaf_id
                 WHERE aml.id = ANY(%(ids)s)
            ), siblings AS (
                SELECT sib.move_id,
                       STRING_AGG(DISTINCT COALESCE(NULLIF(TRIM(leaf_s.cheque_number), ''),
                                                    NULLIF(TRIM(sib.x_cheque_number), '')), ', ') AS all_no
                  FROM account_move_line sib
             LEFT JOIN x_cheque_leaf leaf_s ON leaf_s.id = sib.x_cheque_leaf_id
                 WHERE sib.move_id IN (SELECT move_id FROM own)
              GROUP BY sib.move_id
            )
            SELECT own.id, COALESCE(own.line_no, own.move_no, own.pay_no, siblings.all_no)
              FROM own LEFT JOIN siblings ON siblings.move_id = own.move_id
        """, {'ids': aml_ids})
        return {aml_id: number for aml_id, number in self.env.cr.fetchall() if number}
