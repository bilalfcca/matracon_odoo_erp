from collections import defaultdict

from odoo import api, fields, models


class AccountMoveLine(models.Model):
    _inherit = 'account.move.line'

    x_counterpart_account_display = fields.Char(
        string="Counterpart Account",
        compute='_compute_x_counterpart_account_display',
        help="Account(s) that received the opposite debit/credit side of this journal entry. "
             "When the entry has more than two lines, every other account on the entry is "
             "listed, comma-separated.",
    )

    def _compute_x_counterpart_account_display(self):
        counterpart_by_id = self._get_counterpart_account_display(self.ids)
        for line in self:
            line.x_counterpart_account_display = counterpart_by_id.get(line.id, False)

    @api.model
    def _get_counterpart_account_display(self, aml_ids):
        """Batch-compute a human-readable "code name" label of the counterpart
        account(s) for each account.move.line id in ``aml_ids``.

        The "counterpart" of a line is the account of every *other* line on
        the same journal entry (move) - i.e. the account that received the
        opposite debit/credit side of the transaction. When a move has more
        than two lines, every other account is listed, comma-separated.

        This is shared by this field's compute and by the General Ledger
        report's custom engine (see account_general_ledger.py), so both
        surfaces stay consistent and the underlying query is written once.

        One batched SQL query regardless of how many ids are passed - no
        per-line queries - so this is safe to call from report/list rendering
        on pages of dozens/hundreds of lines.

        :param aml_ids: list of account.move.line ids.
        :return: dict {aml_id: display_string}. Ids with no counterpart
                 (e.g. a line whose move has no other postable line) are
                 omitted from the result.
        """
        aml_ids = [aml_id for aml_id in aml_ids if aml_id]
        if not aml_ids:
            return {}

        self.env.cr.execute("""
            SELECT this_line.id AS line_id, other_line.account_id AS other_account_id
              FROM account_move_line this_line
              JOIN account_move_line other_line
                ON other_line.move_id = this_line.move_id
               AND other_line.id != this_line.id
               AND COALESCE(other_line.display_type, '') NOT IN ('line_section', 'line_note')
             WHERE this_line.id = ANY(%s)
             GROUP BY this_line.id, other_line.account_id
        """, [aml_ids])

        account_ids_by_line = defaultdict(set)
        all_account_ids = set()
        for line_id, other_account_id in self.env.cr.fetchall():
            account_ids_by_line[line_id].add(other_account_id)
            all_account_ids.add(other_account_id)

        if not all_account_ids:
            return {}

        # Resolve code/name through the ORM (not raw SQL) so multi-company
        # code storage (account.account.code is computed from the
        # company-dependent code_store field) and any translations are
        # resolved exactly the way the rest of the UI resolves them.
        accounts = self.env['account.account'].browse(all_account_ids)
        display_by_account_id = {account.id: f"{account.code} {account.name}" for account in accounts}

        result = {}
        for line_id, account_ids in account_ids_by_line.items():
            labels = sorted(
                display_by_account_id[account_id]
                for account_id in account_ids
                if display_by_account_id.get(account_id)
            )
            if labels:
                result[line_id] = ', '.join(labels)
        return result
