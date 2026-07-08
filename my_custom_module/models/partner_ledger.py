from odoo import models


class PartnerLedgerReportHandler(models.AbstractModel):
    _inherit = 'account.partner.ledger.report.handler'

    # ── Column filtering ──────────────────────────────────────────────────────────

    def _custom_options_initializer(self, report, options, previous_options):
        """Remove Due Date and Matching columns — not used in our workflow."""
        super()._custom_options_initializer(report, options, previous_options=previous_options)
        options['columns'] = [
            col for col in options['columns']
            if col['expression_label'] not in ('date_maturity', 'matching_number')
        ]

    # ── Tax amount helpers ────────────────────────────────────────────────────────

    def _get_tax_amounts_by_move(self, moves):
        """Return WHT/Retention breakdown per account.move id.

        Payment journal entries → reads x_tax_line_ids on the linked payment.
        Tax deduction MISC entries → returns zero (their debit IS the tax;
                                     amounts are merged into the payment line).
        Standard invoice/bill entries → reads tax_line_id on individual lines.
        """
        result = {}
        for move in moves:
            wht = 0.0
            retention = 0.0
            other = 0.0
            standard_taxes = 0.0

            payment = move.origin_payment_id
            if payment:
                # Skip WHT column for the tax-deduction MISC entry itself —
                # its amounts are merged into the payment line by
                # _merge_tax_deduction_lines, so the column would double-count.
                if move.id != payment.x_tax_deduction_move_id.id:
                    for tl in payment.x_tax_line_ids:
                        if tl.effect == 'deduct':
                            if tl.tax_type == 'wht':
                                wht += tl.amount
                            elif tl.tax_type == 'retention':
                                retention += tl.amount
                            else:
                                other += tl.amount
            else:
                # Standard bill/entry: aggregate tax line amounts
                for line in move.line_ids:
                    if line.tax_line_id:
                        standard_taxes += abs(line.balance)

            result[move.id] = {
                'taxes_amount': wht + retention + other + standard_taxes,
                'wht_amount': wht,
                'retention_amount': retention,
            }
        return result

    def _inject_tax_columns(self, aml_results):
        """Add WHT / Retention / Total-Deductions columns to partner ledger AML rows."""
        if not aml_results:
            return

        if isinstance(aml_results, dict):
            items = [
                (aml_id, values)
                for aml_id, values in aml_results.items()
                if isinstance(aml_id, int) and isinstance(values, dict)
            ]
        elif isinstance(aml_results, list):
            items = [
                (values.get('id'), values)
                for values in aml_results
                if isinstance(values, dict) and values.get('id')
            ]
        else:
            return

        if not items:
            return

        move_lines = self.env['account.move.line'].browse([aml_id for aml_id, _ in items])
        tax_by_move = self._get_tax_amounts_by_move(move_lines.move_id)

        for aml_id, values in items:
            move_line = move_lines.browse(aml_id)
            if not move_line.exists():
                continue

            taxes = tax_by_move.get(move_line.move_id.id, {})

            # For standard invoice tax lines, show their individual amount.
            if move_line.tax_line_id and not move_line.move_id.origin_payment_id:
                values['taxes_amount'] = abs(move_line.balance)
                values['wht_amount'] = 0.0
                values['retention_amount'] = 0.0
            else:
                values['taxes_amount'] = taxes.get('taxes_amount', 0.0)
                values['wht_amount'] = taxes.get('wht_amount', 0.0)
                values['retention_amount'] = taxes.get('retention_amount', 0.0)

    def _merge_tax_deduction_lines(self, aml_list):
        """Merge WHT/Retention MISC tax-deduction entries into the parent payment line.

        When a vendor payment has WHT deducted, two journal entries are created:
          - The net payment (BOK journal)     debit = cash paid to vendor
          - The tax deduction entry (MISC)    debit = WHT amount (clears AP)

        Both entries appear as separate rows in the partner ledger.
        This method adds the MISC debit/balance into the payment (BOK) row and
        removes the MISC row, leaving ONE clean line per payment showing the full
        gross amount cleared from the vendor's AP.

        Ordering guarantee: the payment move (BOK) is always created before the
        tax-deduction move (MISC), so the BOK AML id < MISC AML id.  The SQL
        orders by (date, id), so BOK is always processed before its MISC entry —
        the lookup map is already populated when we encounter MISC.
        """
        if not aml_list:
            return

        aml_ids = [r['id'] for r in aml_list if isinstance(r, dict) and r.get('id')]
        if not aml_ids:
            return

        # One query: find AMLs that belong to tax-deduction moves, and the
        # payment journal entry move_id they should be merged into.
        self.env.cr.execute("""
            SELECT
                aml.id          AS deduction_aml_id,
                pay_move.id     AS payment_move_id
            FROM account_move_line aml
            JOIN account_payment pay ON pay.x_tax_deduction_move_id = aml.move_id
            JOIN account_move pay_move ON pay_move.id = pay.move_id
            WHERE aml.id = ANY(%s)
        """, (aml_ids,))
        deduction_map = {
            row['deduction_aml_id']: row['payment_move_id']
            for row in self.env.cr.dictfetchall()
        }
        if not deduction_map:
            return

        # Fetch move_ids for non-deduction AMLs so we can find the payment row.
        non_deduction_ids = [aid for aid in aml_ids if aid not in deduction_map]
        if non_deduction_ids:
            self.env.cr.execute(
                "SELECT id, move_id FROM account_move_line WHERE id = ANY(%s)",
                (non_deduction_ids,),
            )
            move_of_aml = {row['id']: row['move_id'] for row in self.env.cr.dictfetchall()}
        else:
            move_of_aml = {}

        # Index: payment_move_id → first AML result dict for that move (the BOK row)
        payment_aml_by_move = {}
        for r in aml_list:
            if not isinstance(r, dict) or not r.get('id'):
                continue
            aml_id = r['id']
            if aml_id in deduction_map:
                continue
            move_id = move_of_aml.get(aml_id)
            if move_id is not None and move_id not in payment_aml_by_move:
                payment_aml_by_move[move_id] = r

        # Merge deduction amounts into the payment AML row; collect rows to remove.
        to_remove = []
        for r in aml_list:
            if not isinstance(r, dict) or not r.get('id'):
                continue
            aml_id = r['id']
            if aml_id not in deduction_map:
                continue
            payment_move_id = deduction_map[aml_id]
            if payment_move_id in payment_aml_by_move:
                pay_r = payment_aml_by_move[payment_move_id]
                pay_r['debit']   = pay_r.get('debit',   0.0) + r.get('debit',   0.0)
                pay_r['credit']  = pay_r.get('credit',  0.0) + r.get('credit',  0.0)
                pay_r['amount']  = pay_r.get('amount',  0.0) + r.get('amount',  0.0)
                pay_r['balance'] = pay_r.get('balance', 0.0) + r.get('balance', 0.0)
            to_remove.append(r)

        for r in to_remove:
            aml_list.remove(r)

    # ── AML value overrides ───────────────────────────────────────────────────────

    def _get_aml_values(self, options, partner_ids, offset=0, limit=None):
        """Inject WHT columns then merge tax-deduction MISC entries."""
        rslt = super()._get_aml_values(options, partner_ids, offset=offset, limit=limit)
        if isinstance(rslt, dict):
            for partner_id in partner_ids:
                if partner_id in rslt:
                    # Inject first so WHT columns are set on the payment row
                    # before the MISC row is removed.
                    self._inject_tax_columns(rslt[partner_id])
                    self._merge_tax_deduction_lines(rslt[partner_id])
        return rslt
