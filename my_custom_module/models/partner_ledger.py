from copy import deepcopy
from datetime import timedelta

from odoo import models, fields as ofields, _
from odoo.exceptions import UserError
from odoo.tools import SQL
from collections import defaultdict

# ── Account type display labels ─────────────────────────────────────────────
_ACCOUNT_TYPE_LABELS = {
    'asset_receivable':    'Accounts Receivable',
    'asset_cash':          'Bank and Cash',
    'asset_current':       'Current Assets',
    'asset_non_current':   'Non-current Assets',
    'asset_prepayments':   'Prepayments',
    'asset_fixed':         'Fixed Assets',
    'liability_payable':   'Accounts Payable',
    'liability_credit_card': 'Credit Card',
    'liability_current':   'Current Liabilities',
    'liability_non_current': 'Non-current Liabilities',
    'equity':              'Equity',
    'equity_unaffected':   'Current Year Earnings',
    'income':              'Income',
    'income_other':        'Other Income',
    'expense':             'Expenses',
    'expense_direct_cost': 'Cost of Revenue',
    'off_balance':         'Off Balance',
}

# Sort order: assets first, then liabilities, equity, income, expense
_ACCOUNT_TYPE_ORDER = {
    'asset_receivable': 1,
    'asset_cash': 2,
    'asset_current': 3,
    'asset_non_current': 4,
    'asset_prepayments': 5,
    'asset_fixed': 6,
    'liability_payable': 7,
    'liability_credit_card': 8,
    'liability_current': 9,
    'liability_non_current': 10,
    'equity': 11,
    'equity_unaffected': 12,
    'income': 13,
    'income_other': 14,
    'expense': 15,
    'expense_direct_cost': 16,
    'off_balance': 17,
}


class PartnerLedgerReportHandler(models.AbstractModel):
    _inherit = 'account.partner.ledger.report.handler'

    # ── Column filtering ──────────────────────────────────────────────────────

    def _custom_options_initializer(self, report, options, previous_options):
        """Remove Due Date and Matching columns — not used in our workflow."""
        super()._custom_options_initializer(report, options, previous_options=previous_options)
        options['columns'] = [
            col for col in options['columns']
            if col['expression_label'] not in ('date_maturity', 'matching_number')
        ]

    # ── Tax amount helpers ────────────────────────────────────────────────────

    def _get_tax_amounts_by_move(self, moves):
        """Return WHT/Retention breakdown per account.move id."""
        result = {}
        for move in moves:
            wht = 0.0
            retention = 0.0
            other = 0.0
            standard_taxes = 0.0

            payment = move.origin_payment_id
            if payment:
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

            if move_line.tax_line_id and not move_line.move_id.origin_payment_id:
                values['taxes_amount'] = abs(move_line.balance)
                values['wht_amount'] = 0.0
                values['retention_amount'] = 0.0
            else:
                values['taxes_amount'] = taxes.get('taxes_amount', 0.0)
                values['wht_amount'] = taxes.get('wht_amount', 0.0)
                values['retention_amount'] = taxes.get('retention_amount', 0.0)

    def _merge_tax_deduction_lines(self, aml_list):
        """Merge WHT/Retention MISC tax-deduction entries into the parent payment line."""
        if not aml_list:
            return

        aml_ids = [r['id'] for r in aml_list if isinstance(r, dict) and r.get('id')]
        if not aml_ids:
            return

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

        non_deduction_ids = [aid for aid in aml_ids if aid not in deduction_map]
        if non_deduction_ids:
            self.env.cr.execute(
                "SELECT id, move_id FROM account_move_line WHERE id = ANY(%s)",
                (non_deduction_ids,),
            )
            move_of_aml = {row['id']: row['move_id'] for row in self.env.cr.dictfetchall()}
        else:
            move_of_aml = {}

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

    # ── Account type injection ────────────────────────────────────────────────

    def _inject_account_type(self, rslt):
        """Add account_type to each AML result dict (single SQL lookup)."""
        all_account_ids = set()
        for aml_list in rslt.values():
            for aml in aml_list:
                if isinstance(aml, dict) and aml.get('account_id'):
                    all_account_ids.add(aml['account_id'])
        if not all_account_ids:
            return
        self.env.cr.execute(
            "SELECT id, account_type, code, name FROM account_account WHERE id = ANY(%s)",
            (list(all_account_ids),)
        )
        acc_info = {r['id']: r for r in self.env.cr.dictfetchall()}
        for aml_list in rslt.values():
            for aml in aml_list:
                if isinstance(aml, dict) and aml.get('account_id'):
                    info = acc_info.get(aml['account_id'], {})
                    aml['account_type'] = info.get('account_type', 'other')

    # ── AML value overrides ───────────────────────────────────────────────────

    def _get_aml_values(self, options, partner_ids, offset=0, limit=None):
        """Inject WHT columns, merge tax-deduction entries, and add account_type."""
        rslt = super()._get_aml_values(options, partner_ids, offset=offset, limit=limit)
        if isinstance(rslt, dict):
            for partner_id in partner_ids:
                if partner_id in rslt:
                    self._inject_tax_columns(rslt[partner_id])
                    self._merge_tax_deduction_lines(rslt[partner_id])
            self._inject_account_type(rslt)
        return rslt

    # ── Column builder for summary rows ──────────────────────────────────────

    def _build_pl_group_columns(self, report, options, cgk_data, account_code=None):
        """Build column values for account_type and account summary rows.

        cgk_data: {column_group_key: {'debit': float, 'credit': float, 'balance': float, ...}}
        """
        columns = []
        for col in options['columns']:
            cgk = col['column_group_key']
            expr = col['expression_label']
            data = cgk_data.get(cgk, {})

            if expr == 'debit':
                val = data.get('debit', 0.0)
            elif expr == 'credit':
                val = data.get('credit', 0.0)
            elif expr == 'balance':
                val = data.get('balance', 0.0)
            elif expr == 'wht_amount':
                val = data.get('wht_amount', 0.0)
            elif expr == 'retention_amount':
                val = data.get('retention_amount', 0.0)
            elif expr == 'taxes_amount':
                val = data.get('taxes_amount', 0.0)
            elif expr == 'account_code' and account_code:
                val = account_code
            else:
                val = None  # blank for date/string columns

            columns.append(report._build_column_dict(val, col, options=options))
        return columns

    # ── Group AMLs by account_type and account_id ────────────────────────────

    def _group_amls_by_type_and_account(self, aml_results):
        """Return {account_type: {account_id: {cgk: {totals}, 'account_code': str, 'account_name': str}}}."""
        type_groups = {}
        for aml in aml_results:
            at = aml.get('account_type', 'other')
            aid = aml.get('account_id')
            cgk = aml.get('column_group_key', '')

            if at not in type_groups:
                type_groups[at] = {}
            if aid not in type_groups[at]:
                type_groups[at][aid] = {
                    '_account_code': aml.get('account_code', ''),
                    '_account_name': aml.get('account_name', ''),
                }
            acct = type_groups[at][aid]
            if cgk not in acct:
                acct[cgk] = {'debit': 0.0, 'credit': 0.0, 'balance': 0.0,
                              'wht_amount': 0.0, 'retention_amount': 0.0, 'taxes_amount': 0.0}
            acct[cgk]['debit']            += aml.get('debit', 0.0)
            acct[cgk]['credit']           += aml.get('credit', 0.0)
            acct[cgk]['balance']          += aml.get('balance', 0.0)
            acct[cgk]['wht_amount']       += aml.get('wht_amount', 0.0)
            acct[cgk]['retention_amount'] += aml.get('retention_amount', 0.0)
            acct[cgk]['taxes_amount']     += aml.get('taxes_amount', 0.0)
        return type_groups

    def _sum_type_cgk(self, type_group):
        """Sum all account-level data into type-level data per cgk."""
        type_cgk = {}
        for aid, acct_data in type_group.items():
            for cgk, vals in acct_data.items():
                if cgk.startswith('_'):
                    continue
                if cgk not in type_cgk:
                    type_cgk[cgk] = {'debit': 0.0, 'credit': 0.0, 'balance': 0.0,
                                     'wht_amount': 0.0, 'retention_amount': 0.0, 'taxes_amount': 0.0}
                for key in type_cgk[cgk]:
                    type_cgk[cgk][key] += vals.get(key, 0.0)
        return type_cgk

    # ── Initial balance for a specific partner + account ─────────────────────

    def _get_initial_balance_for_account(self, partner_id, account_id, options):
        """Like _get_initial_balance_values but filtered to one account_id.

        Returns {cgk: {'debit': 0, 'credit': 0, 'balance': float, 'amount': float}}
        """
        report = self.env['account.report'].browse(options['report_id'])
        if not report.filter_date_range:
            return {cgk: defaultdict(float) for cgk in options['column_groups']}

        result = {cgk: defaultdict(float) for cgk in options['column_groups']}
        queries = []
        for column_group_key, column_group_options in report._split_options_per_column_group(options).items():
            new_options = deepcopy(column_group_options)
            new_date_to = ofields.Date.from_string(new_options['date']['date_from']) - timedelta(days=1)
            new_options['date']['date_from'] = False
            new_options['date']['date_to'] = ofields.Date.to_string(new_date_to)
            for cg in new_options['column_groups'].values():
                cg['forced_options']['date'] = new_options['date']

            domain = [('account_id', '=', account_id)]
            if partner_id is not None:
                domain.append(('partner_id', '=', partner_id))
            else:
                domain.append(('partner_id', '=', False))

            query = report._get_report_query(new_options, 'from_beginning', domain=domain)
            queries.append(SQL(
                """
                SELECT
                    %(column_group_key)s    AS column_group_key,
                    0                       AS debit,
                    0                       AS credit,
                    SUM(%(balance_select)s) AS amount,
                    SUM(%(balance_select)s) AS balance
                FROM %(table_references)s
                %(currency_table_join)s
                WHERE %(search_condition)s
                """,
                column_group_key=column_group_key,
                balance_select=report._currency_table_apply_rate(SQL("account_move_line.balance")),
                table_references=query.from_clause,
                currency_table_join=report._currency_table_aml_join(new_options),
                search_condition=query.where_clause,
            ))

        if queries:
            self.env.cr.execute(SQL(" UNION ALL ").join(queries))
            for row in self.env.cr.dictfetchall():
                result[row['column_group_key']]['debit']   = row['debit']
                result[row['column_group_key']]['credit']  = row['credit']
                result[row['column_group_key']]['balance'] = row['balance']
                result[row['column_group_key']]['amount']  = row['amount']

        return result

    # ── LEVEL 1 expand: Partner → Account Type rows ───────────────────────────

    def _report_expand_unfoldable_line_partner_ledger(self, line_dict_id, groupby, options, progress, offset, unfold_all_batch_data=None):
        """Override: expand partner line into account-type grouping rows."""
        report = self.env['account.report'].browse(options['report_id'])
        parsed = report._parse_line_id(line_dict_id)
        markup, model, record_id = parsed[-1]

        if model != 'res.partner':
            raise UserError(_("Wrong ID for partner ledger line to expand: %s", line_dict_id))

        partner_id = record_id  # may be None for unknown partner

        prefix_groups_count = sum(
            1 for mk, _m, _r in parsed
            if isinstance(mk, dict) and 'groupby_prefix_group' in mk
        )
        level_shift = prefix_groups_count * 2

        # Fetch all AMLs for this partner
        if unfold_all_batch_data:
            aml_results = unfold_all_batch_data['aml_values'].get(partner_id, [])
        else:
            aml_results = self._get_aml_values(options, [partner_id]).get(partner_id, [])

        # Group by account_type → account_id
        type_groups = self._group_amls_by_type_and_account(aml_results)

        lines = []
        for account_type in sorted(type_groups.keys(), key=lambda x: _ACCOUNT_TYPE_ORDER.get(x, 99)):
            type_cgk = self._sum_type_cgk(type_groups[account_type])
            at_line_id = report._get_generic_line_id(
                None, None,
                markup={'pl_account_type': account_type},
                parent_line_id=line_dict_id,
            )
            columns = self._build_pl_group_columns(report, options, type_cgk)
            at_line = {
                'id': at_line_id,
                'parent_id': line_dict_id,
                'name': _ACCOUNT_TYPE_LABELS.get(account_type, account_type),
                'columns': columns,
                'level': 2 + level_shift,
                'unfoldable': True,
                'unfolded': at_line_id in options['unfolded_lines'] or options['unfold_all'],
                'expand_function': '_report_expand_unfoldable_line_pl_account_type',
                'class': 'font-italic',
            }
            lines.append(at_line)

        return {
            'lines': lines,
            'offset_increment': len(lines),
            'has_more': False,
            'progress': progress,
        }

    # ── LEVEL 2 expand: Account Type → Account rows ───────────────────────────

    def _report_expand_unfoldable_line_pl_account_type(self, line_dict_id, groupby, options, progress, offset, unfold_all_batch_data=None):
        """Expand an account-type row into individual account (GL) rows."""
        report = self.env['account.report'].browse(options['report_id'])
        parsed = report._parse_line_id(line_dict_id)

        # Extract account_type from last element's markup
        last_markup, _model, _record_id = parsed[-1]
        if not isinstance(last_markup, dict) or 'pl_account_type' not in last_markup:
            return {'lines': [], 'offset_increment': 0, 'has_more': False}
        account_type = last_markup['pl_account_type']

        # Extract partner_id by searching up the chain
        partner_id = None
        for mk, mdl, rid in reversed(parsed):
            if mdl == 'res.partner':
                partner_id = rid
                break

        prefix_groups_count = sum(
            1 for mk, _m, _r in parsed
            if isinstance(mk, dict) and 'groupby_prefix_group' in mk
        )
        level_shift = prefix_groups_count * 2

        # Re-fetch AMLs for this partner (re-uses the cached batch if available)
        if unfold_all_batch_data:
            aml_results = unfold_all_batch_data['aml_values'].get(partner_id, [])
        else:
            aml_results = self._get_aml_values(options, [partner_id]).get(partner_id, [])

        # Filter to this account_type and group by account_id
        type_groups = self._group_amls_by_type_and_account(aml_results)
        account_group = type_groups.get(account_type, {})

        lines = []
        for account_id, acct_data in sorted(account_group.items(), key=lambda x: x[1].get('_account_code', '')):
            acct_code = acct_data.get('_account_code', '')
            acct_name = acct_data.get('_account_name', '')
            cgk_data = {k: v for k, v in acct_data.items() if not k.startswith('_')}

            acct_line_id = report._get_generic_line_id(
                'account.account', account_id,
                parent_line_id=line_dict_id,
            )
            columns = self._build_pl_group_columns(report, options, cgk_data, account_code=acct_code)
            acct_line = {
                'id': acct_line_id,
                'parent_id': line_dict_id,
                'name': f'{acct_code} {acct_name}'.strip() if acct_code else acct_name,
                'columns': columns,
                'level': 3 + level_shift,
                'unfoldable': True,
                'unfolded': acct_line_id in options['unfolded_lines'] or options['unfold_all'],
                'expand_function': '_report_expand_unfoldable_line_pl_account',
            }
            lines.append(acct_line)

        return {
            'lines': lines,
            'offset_increment': len(lines),
            'has_more': False,
            'progress': progress,
        }

    # ── LEVEL 3 expand: Account → Initial Balance + AML rows ─────────────────

    def _report_expand_unfoldable_line_pl_account(self, line_dict_id, groupby, options, progress, offset, unfold_all_batch_data=None):
        """Expand an account row into initial balance + journal entry rows."""
        report = self.env['account.report'].browse(options['report_id'])
        parsed = report._parse_line_id(line_dict_id)

        # Last element should be (None/markup, 'account.account', account_id)
        last_markup, last_model, account_id = parsed[-1]
        if last_model != 'account.account' or not account_id:
            return {'lines': [], 'offset_increment': 0, 'has_more': False}

        # Find partner_id by searching the line ID chain
        partner_id = report._get_res_id_from_line_id(line_dict_id, 'res.partner')

        prefix_groups_count = sum(
            1 for mk, _m, _r in parsed
            if isinstance(mk, dict) and 'groupby_prefix_group' in mk
        )
        level_shift = prefix_groups_count * 2 + 1  # +1 because we are one level deeper than partner

        lines = []

        # ── Initial Balance ──────────────────────────────────────────────────
        if offset == 0 and not options.get('hide_initial_balance'):
            init_balance_by_col_group = self._get_initial_balance_for_account(partner_id, account_id, options)
            initial_balance_line = report._get_partner_and_general_ledger_initial_balance_line(
                options, line_dict_id, init_balance_by_col_group, level_shift=level_shift
            )
            if initial_balance_line:
                for column in initial_balance_line['columns']:
                    if column.get('expression_label') in ('debit', 'credit'):
                        column['blank_if_zero'] = True
                lines.append(initial_balance_line)
                progress = self._init_load_more_progress(options, initial_balance_line)

        # ── AML rows — get all for partner then filter by account_id in Python ─
        if unfold_all_batch_data:
            all_amls = unfold_all_batch_data['aml_values'].get(partner_id, [])
        else:
            all_amls = self._get_aml_values(options, [partner_id]).get(partner_id, [])

        aml_results = [a for a in all_amls if a.get('account_id') == account_id]

        # Apply load_more pagination on the filtered list
        limit_to_load = report.load_more_limit + 1 if report.load_more_limit and options['export_mode'] != 'print' else None
        if offset:
            aml_results = aml_results[offset:]
        if limit_to_load:
            aml_results = aml_results[:limit_to_load]

        # Use the parent's line builder for AML rows
        # partner_line_id is used for caret options parent context
        aml_report_lines, next_progress, treated_results_count, has_more = self._get_partner_aml_report_lines(
            report, options, line_dict_id, aml_results, progress, offset=offset, level_shift=level_shift
        )
        lines.extend(aml_report_lines)

        return {
            'lines': lines,
            'offset_increment': treated_results_count,
            'has_more': has_more,
            'progress': next_progress,
        }
