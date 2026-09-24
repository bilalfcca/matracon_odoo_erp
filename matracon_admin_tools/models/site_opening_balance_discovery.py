"""Phase 1 (Discovery & Validation) tool for the "Import Site Opening
Inventory Balances" project.

READ-ONLY BY DESIGN. This wizard never creates, writes or unlinks any
business record (product, account, stock move, quant, journal entry...).
The only things it ever writes are its own transient fields. It is meant to
be run first in Staging (against a copy of production data) and, once its
report has been reviewed, again in Production — the exact same tool, same
button, no code difference between the two runs. Nothing here decides or
changes an accounting outcome; it only reports what already exists so a
human can review it before Phase 2 (opening balance import), Phase 3
(historical receipt correction) or Phase 4 (FIFO alignment) touches
anything.

Each site provides its own "MAIN SUMMARY" workbook (see the task); this
wizard is written generically (site + file are both inputs) so the exact
same tool is reused for every site as each new file arrives, not just MCH.

What it reports, per site:
  1. Current costing method / valuation account per product category
     (Odoo 19: product.category.property_cost_method / property_valuation /
     property_stock_valuation_account_id / property_stock_journal), plus the
     company-level fallback.
  2. This site's own GL account wiring already in the code
     (x.project.site.config.x_material_issue_account_id and the
     valuation_account_id already pushed onto its Employee/Subcontractor
     issue locations - see _sync_material_issue_location_accounts), and the
     warehouse's receiving/stock location valuation_account_id (if any).
  3. Item-by-item mapping of every non-zero "Balance Material Amount" row in
     the sheet's MAIN SUMMARY tab against existing product.product records
     (exact / fuzzy / no match), so unmatched descriptions can be flagged
     for a product-creation decision before Phase 2.
  4. On-hand quantity in the system today for exactly-matched products,
     compared to the sheet's Balance Material qty - flagging any product
     that already has stock in the system (double-counting risk if the
     opening balance is imported on top of it).
  5. Every Purchase-Order-originated Receipt (stock.picking, done, this
     site's warehouse incoming picking type, purchase_id set) since a given
     cutoff date, with the GL account its value actually posted to versus
     the account the product's category is currently configured to use -
     flagging entries with no valuation entry at all or a mismatched
     account.

The "Stock" and "Issue" sheets in the workbook are the raw historical
transaction log (support detail only) and are intentionally not read here -
only "MAIN SUMMARY" is, per the task description.
"""
import base64
import csv
import io
import re
from collections import defaultdict

from odoo import api, fields, models, _
from odoo.exceptions import UserError


def _normalize(text):
    """Loose normalization for description matching: upper-case, collapse
    all punctuation/whitespace runs to single spaces. Good enough to catch
    'Deformed Steel Bar 10MM' vs 'DEFORMED STEEL BAR 10 MM' style drift
    without pulling in a fuzzy-matching dependency."""
    if not text:
        return ''
    text = re.sub(r'[^A-Za-z0-9]+', ' ', str(text).upper())
    return re.sub(r'\s+', ' ', text).strip()


class XSiteOpeningBalanceDiscoveryWizard(models.TransientModel):
    _name = 'x.site.opening.balance.discovery.wizard'
    _description = 'Site Opening Balance Import — Discovery Report (Phase 1, read-only)'

    site_config_id = fields.Many2one(
        'x.project.site.config', string='Site', required=True,
        help='Which site this MAIN SUMMARY workbook belongs to.',
    )
    summary_file = fields.Binary(
        string='MAIN SUMMARY file (.xlsx)', attachment=False,
        help='Optional. The site\'s stock/inventory workbook. Only the "MAIN SUMMARY" '
             'sheet is read - the "Stock" and "Issue" sheets are raw historical logs '
             '(support detail only) and are ignored by this tool. Leave empty to run '
             'ONLY the category/site-wiring config and the Purchase Receipts GL audit '
             'below (e.g. to check receipts since July 1 for a site whose opening-balance '
             'workbook has not arrived yet) - sections 1/2/5 (sheet items, product '
             'matching, on-hand cross-check) are then skipped.',
    )
    summary_filename = fields.Char(string='Filename')
    as_of_date = fields.Date(
        string='Balance As Of', default=fields.Date.context_today,
        help='The date the sheet\'s balance is struck as of (e.g. 30 June 2026). Only '
             'used to help you pick the Receipts-Since cutoff below - this tool does '
             'not date or change any record. Not required if no workbook is uploaded.',
    )
    receipts_since_date = fields.Date(
        string='Review Receipts Since', required=True,
        default=lambda self: fields.Date.context_today(self),
        help='Purchase Receipts done on/after this date, up to today, are included in '
             'the GL account audit below - i.e. "since July 1 till date" is just '
             'July 1 here with no end date. Defaults to the day after Balance As Of.',
    )
    value_tolerance = fields.Float(
        string='Value Tolerance (PKR)', default=0.01,
        help='Sheet rows whose Balance Material Amount is below this absolute value '
             'are treated as closed/zero and excluded from the import candidate list.',
    )
    target_account_code = fields.Char(
        string='Target Inventory Account Code', default='230400000',
        help='The ONE shared GL account both the opening balance (Phase 2) and every '
             'Purchase Receipt (Phase 3 correction) must land on, per the confirmed '
             'decision — account 230400000 "Inventory" (Current Asset), used for every '
             'site (no per-site inventory account). Looked up by its Code on '
             'account.account; every category and every receipt below is checked '
             'against it directly, regardless of what is currently configured.',
    )

    result_summary = fields.Text(string='Summary', readonly=True)
    report_file = fields.Binary(string='Detail Report (CSV)', readonly=True, attachment=False)
    report_filename = fields.Char(string='Report Filename', readonly=True)

    @api.onchange('as_of_date')
    def _onchange_as_of_date(self):
        for wizard in self:
            if wizard.as_of_date and not wizard.receipts_since_date:
                wizard.receipts_since_date = wizard.as_of_date

    def _check_access(self):
        if not (self.env.user.has_group('purchase_demand_raise.group_matracon_admin')
                or self.env.user.has_group('site_operations.group_finance_ho')
                or self.env.user.has_group('base.group_system')):
            raise UserError(_('Only Matracon Admin, Finance HO or System Administrator can run this report.'))

    # ── 1. Parse the MAIN SUMMARY sheet ───────────────────────────────────

    def _parse_main_summary(self):
        self.ensure_one()
        try:
            import openpyxl
        except ImportError:
            raise UserError(_('openpyxl is not available in this environment - cannot read the workbook.'))
        if not self.summary_file:
            raise UserError(_('Please attach the site\'s workbook (.xlsx) first.'))

        wb = openpyxl.load_workbook(io.BytesIO(base64.b64decode(self.summary_file)), data_only=True)
        sheet = None
        for name in wb.sheetnames:
            if name.strip().upper() == 'MAIN SUMMARY':
                sheet = wb[name]
                break
        if sheet is None:
            raise UserError(_(
                'No sheet named "MAIN SUMMARY" found in this file. Sheets found: %s'
            ) % ', '.join(wb.sheetnames))

        items = []
        category = None
        for row in sheet.iter_rows(min_row=1, values_only=True):
            if not row or all(v is None for v in row):
                continue
            sr = row[0]
            desc = row[1] if len(row) > 1 else None
            # A category header row is a bare text label in column A with
            # nothing in the Description column (e.g. "Steel", "Cables").
            if isinstance(sr, str) and desc is None:
                category = sr.strip()
                continue
            # An item row has a numeric Sr# and a description; anything else
            # (title rows, the column-header row itself, blank spacers) is skipped.
            if not isinstance(sr, (int, float)) or not desc:
                continue
            unit = row[2] if len(row) > 2 else None
            bal_qty = row[5] if len(row) > 5 else None
            bal_value = row[7] if len(row) > 7 else None
            items.append({
                'category': category or '(uncategorized)',
                'description': str(desc).strip(),
                'unit': unit or '',
                'qty': bal_qty if isinstance(bal_qty, (int, float)) else 0.0,
                'value': bal_value if isinstance(bal_value, (int, float)) else 0.0,
            })
        return items

    # ── 2. Category / company valuation configuration ─────────────────────

    def _resolve_target_account(self):
        """The single confirmed target account (e.g. 230400000 'Inventory') that
        BOTH the opening balance import and every Purchase Receipt must land on.
        Looked up by Code so this keeps working unchanged wherever it runs
        (Staging, Production) as long as the account exists with that code."""
        code = (self.target_account_code or '').strip()
        if not code:
            return self.env['account.account']
        return self.env['account.account'].sudo().search([
            ('code', '=', code),
            ('company_ids', 'in', self.env.company.id),
        ], limit=1)

    def _category_config_rows(self, target_account):
        rows = []
        for categ in self.env['product.category'].sudo().search([]):
            acc = categ.property_stock_valuation_account_id
            rows.append({
                'category': categ.display_name,
                'cost_method': categ.property_cost_method or '',
                'valuation': categ.property_valuation or '',
                'valuation_account': f'{acc.code} {acc.name}' if acc else '',
                'on_target': bool(target_account and acc and acc.id == target_account.id),
                'stock_variation_account': (
                    acc.account_stock_variation_id.display_name
                    if acc and acc.account_stock_variation_id else ''
                ),
                'stock_journal': categ.property_stock_journal.display_name if categ.property_stock_journal else '',
            })
        return rows

    def _company_defaults(self):
        company = self.env.company
        return {
            'cost_method': company.cost_method or '',
            'inventory_valuation': company.inventory_valuation or '',
            'account_stock_valuation_id': (
                company.account_stock_valuation_id.display_name if company.account_stock_valuation_id else ''
            ),
            'account_stock_journal_id': (
                company.account_stock_journal_id.display_name if company.account_stock_journal_id else ''
            ),
        }

    def _site_account_rows(self, site_config):
        warehouse = site_config.warehouse_id
        rows = []

        def acc_label(loc):
            return f'{loc.valuation_account_id.code} {loc.valuation_account_id.name}' if loc and loc.valuation_account_id else '(not set — falls back to product category account)'

        rows.append(('Site', site_config.display_name))
        rows.append(('Warehouse', warehouse.display_name if warehouse else '(none configured)'))
        rows.append(('Analytic Account', site_config.analytic_account_id.display_name or ''))
        rows.append(('Material Issue Account (x_material_issue_account_id)',
                      site_config.x_material_issue_account_id.display_name
                      if site_config.x_material_issue_account_id else '(not set)'))
        rows.append(('Employee Issue Location — Valuation Account',
                      acc_label(site_config.x_employee_location_id)))
        rows.append(('Subcontractor Issue Location — Valuation Account',
                      acc_label(site_config.x_subcontractor_location_id)))
        if warehouse:
            rows.append(('Warehouse Stock Location — Valuation Account',
                          acc_label(warehouse.lot_stock_id)))
            if warehouse.in_type_id and warehouse.in_type_id.default_location_dest_id:
                rows.append(('Receiving Location — Valuation Account',
                              acc_label(warehouse.in_type_id.default_location_dest_id)))
        return rows

    # ── 3. Item → product matching ─────────────────────────────────────────

    def _match_products(self, items):
        Product = self.env['product.product'].sudo()
        product_data = Product.search_read([], ['id', 'name', 'default_code', 'categ_id'])
        index = defaultdict(list)
        for p in product_data:
            index[_normalize(p['name'])].append(p)

        # Fuzzy pass only makes sense on a catalog small enough to scan
        # exhaustively in reasonable time; above that, only exact matching runs
        # and everything else is reported as "no match" for manual review.
        allow_fuzzy = len(index) <= 6000

        results = []
        for item in items:
            norm = _normalize(item['description'])
            matched = index.get(norm) or []
            status = 'exact' if matched else 'none'
            if not matched and allow_fuzzy and norm:
                candidates = []
                for key, plist in index.items():
                    if key and (norm in key or key in norm):
                        candidates.extend(plist)
                if candidates:
                    status = 'fuzzy'
                    matched = candidates[:5]
            row = dict(item)
            row['match_status'] = status
            row['matched_products'] = matched
            results.append(row)
        return results

    # ── 4. On-hand cross-check (exact single-match items only) ────────────

    def _on_hand_rows(self, matched_items, site_config):
        warehouse = site_config.warehouse_id
        location = warehouse.view_location_id if warehouse else False
        rows = []
        if not location:
            return rows
        Quant = self.env['stock.quant'].sudo()
        for item in matched_items:
            if item['match_status'] != 'exact' or len(item['matched_products']) != 1:
                continue
            product = item['matched_products'][0]
            quants = Quant.search([
                ('product_id', '=', product['id']),
                ('location_id', 'child_of', location.id),
            ])
            system_qty = sum(quants.mapped('quantity'))
            rows.append({
                'category': item['category'],
                'description': item['description'],
                'sheet_qty': item['qty'],
                'system_qty': system_qty,
                'diff': system_qty - item['qty'],
            })
        return rows

    # ── 5. PO receipts GL audit ────────────────────────────────────────────

    def _receipts_audit(self, site_config, since_date, target_account):
        rows = []
        summary = {
            'pickings': 0, 'moves': 0, 'no_gl': 0, 'mismatch': 0, 'ok': 0, 'total_value': 0.0,
            'needs_correction_value': 0.0,
        }
        warehouse = site_config.warehouse_id
        if not warehouse or not warehouse.in_type_id:
            return rows, summary

        pickings = self.env['stock.picking'].sudo().search([
            ('picking_type_id', '=', warehouse.in_type_id.id),
            ('state', '=', 'done'),
            ('purchase_id', '!=', False),
            ('date_done', '>=', since_date),
        ])
        summary['pickings'] = len(pickings)

        for picking in pickings:
            for move in picking.move_ids.filtered(lambda m: m.state == 'done' and m.product_id):
                summary['moves'] += 1
                summary['total_value'] += move.value or 0.0
                categ = move.product_id.categ_id
                configured = move.product_id._get_product_accounts().get('stock_valuation')
                status = 'OK — ALREADY ON TARGET'
                actual_accounts = ''
                on_target = False
                if not move.account_move_id:
                    status = 'NO GL ENTRY — needs entry on target account'
                    summary['no_gl'] += 1
                    summary['needs_correction_value'] += move.value or 0.0
                else:
                    je_accounts = move.account_move_id.line_ids.account_id
                    actual_accounts = ', '.join(f'{a.code} {a.name}' for a in je_accounts)
                    on_target = bool(target_account and target_account.id in je_accounts.ids)
                    if on_target:
                        summary['ok'] += 1
                    else:
                        status = 'NEEDS CORRECTION — not on target account'
                        summary['mismatch'] += 1
                        summary['needs_correction_value'] += move.value or 0.0
                rows.append({
                    'picking': picking.name,
                    'date': picking.date_done,
                    'product': move.product_id.display_name,
                    'category': categ.display_name,
                    'cost_method': categ.property_cost_method or move.product_id.cost_method or '',
                    'qty': move.quantity,
                    'value': move.value,
                    'configured_account': f'{configured.code} {configured.name}' if configured else '(unresolved)',
                    'target_account': f'{target_account.code} {target_account.name}' if target_account else '(target account not found)',
                    'actual_accounts_on_je': actual_accounts,
                    'status': status,
                })
        return rows, summary

    # ── Assemble & run ─────────────────────────────────────────────────────

    def action_run_discovery(self):
        self.ensure_one()
        self._check_access()
        site_config = self.site_config_id
        has_file = bool(self.summary_file)

        items = self._parse_main_summary() if has_file else []
        candidates = [i for i in items if abs(i['value']) > self.value_tolerance]
        zero_value_with_qty = [
            i for i in items
            if abs(i['value']) <= self.value_tolerance and abs(i['qty']) > 1e-6
        ]
        matched_items = self._match_products(candidates) if has_file else []
        exact_n = sum(1 for i in matched_items if i['match_status'] == 'exact')
        fuzzy_n = sum(1 for i in matched_items if i['match_status'] == 'fuzzy')
        none_n = sum(1 for i in matched_items if i['match_status'] == 'none')
        total_value = sum(i['value'] for i in candidates)

        target_account = self._resolve_target_account()
        categ_rows = self._category_config_rows(target_account)
        company_defaults = self._company_defaults()
        site_account_rows = self._site_account_rows(site_config)
        on_hand_rows = self._on_hand_rows(matched_items, site_config) if has_file else []
        on_hand_conflicts = [r for r in on_hand_rows if abs(r['system_qty']) > 1e-6]
        receipts_rows, receipts_summary = self._receipts_audit(
            site_config, self.receipts_since_date, target_account)

        non_fifo_categs = [r['category'] for r in categ_rows if r['cost_method'] and r['cost_method'] != 'fifo']
        off_target_categs = [r['category'] for r in categ_rows if not r['on_target']]

        # ── Summary text ───────────────────────────────────────────────
        lines = []
        lines.append(f'=== Site Opening Balance — Discovery Report ===')
        lines.append(f'Site: {site_config.display_name}')
        lines.append(
            f'Target Inventory Account (confirmed — one shared account for all sites, used '
            f'for both the opening balance and every Purchase Receipt): '
            f'{target_account.code + " " + target_account.name if target_account else self.target_account_code + " — NOT FOUND in this database"}'
        )
        if has_file:
            lines.append(f'Workbook: {self.summary_filename or "(unnamed)"}')
            lines.append(f'Balance As Of: {self.as_of_date}')
        else:
            lines.append('No MAIN SUMMARY workbook uploaded — running the Purchase Receipts '
                          'GL audit and current config check only (sections 1/2/5 below are skipped).')
        lines.append('')
        if has_file:
            lines.append('--- 1. MAIN SUMMARY sheet ---')
            lines.append(f'Total item rows read: {len(items)}')
            lines.append(f'Non-zero Balance Material Amount rows (import candidates): {len(candidates)}')
            lines.append(f'Total candidate value: {total_value:,.2f}')
            if zero_value_with_qty:
                lines.append(
                    f'NOTE: {len(zero_value_with_qty)} item(s) have a non-zero Balance Material qty but '
                    f'zero Balance Material Amount — excluded from the value total above but still represent '
                    f'physical stock; review separately (see detail CSV).'
                )
            lines.append('')
            lines.append('--- 2. Product matching ---')
            lines.append(f'Exact match: {exact_n}')
            lines.append(f'Fuzzy match (needs manual confirmation): {fuzzy_n}')
            lines.append(f'No match found (needs product creation/mapping decision): {none_n}')
            lines.append('')
        lines.append('--- 3. Costing method / valuation (product.category) ---')
        lines.append(
            f'Company default: cost_method={company_defaults["cost_method"]}, '
            f'inventory_valuation={company_defaults["inventory_valuation"]}, '
            f'stock valuation account={company_defaults["account_stock_valuation_id"] or "(none)"}, '
            f'stock journal={company_defaults["account_stock_journal_id"] or "(none)"}'
        )
        lines.append(f'{len(categ_rows)} product categories found — see detail CSV for the full per-category table.')
        if non_fifo_categs:
            lines.append(
                f'NOT currently set to FIFO ({len(non_fifo_categs)} categor{"y" if len(non_fifo_categs)==1 else "ies"}): '
                + ', '.join(non_fifo_categs)
            )
        else:
            lines.append('All categories with an explicit costing method are already set to FIFO.')
        if off_target_categs:
            lines.append(
                f'NOT currently pointing to the target inventory account ({self.target_account_code}) '
                f'({len(off_target_categs)} of {len(categ_rows)}): ' + ', '.join(off_target_categs)
                + ' — these would need their Stock Valuation Account changed to the target account '
                  'as part of Phase 2/4 (this report only flags it, it changes nothing).'
            )
        else:
            lines.append(f'All categories already point to the target inventory account ({self.target_account_code}).')
        lines.append('')
        lines.append('--- 4. Site GL account wiring ---')
        for label, value in site_account_rows:
            lines.append(f'{label}: {value}')
        lines.append('')
        if has_file:
            lines.append('--- 5. On-hand cross-check (exact single-match items only) ---')
            lines.append(f'Checked: {len(on_hand_rows)} matched product(s)')
            lines.append(f'Already have system on-hand quantity (double-count risk): {len(on_hand_conflicts)}')
            lines.append('')
        lines.append(f'--- 6. Purchase Receipts audit (since {self.receipts_since_date}, vs. target account {self.target_account_code}) ---')
        lines.append(f'Receipts (done, PO-linked) reviewed: {receipts_summary["pickings"]}')
        lines.append(f'Stock moves reviewed: {receipts_summary["moves"]}')
        lines.append(f'Total receipt value: {receipts_summary["total_value"]:,.2f}')
        lines.append(f'Already posted to the target account: {receipts_summary["ok"]}')
        lines.append(f'Posted to a DIFFERENT account (needs Phase 3 correction): {receipts_summary["mismatch"]}')
        lines.append(f'No GL entry at all (needs Phase 3 entry): {receipts_summary["no_gl"]}')
        lines.append(
            f'Total value that Phase 3 would need to move onto the target account: '
            f'{receipts_summary["needs_correction_value"]:,.2f}'
        )
        lines.append('')
        lines.append('Full row-level detail (all candidate items, all categories, on-hand cross-check, '
                      'and every reviewed receipt) is in the attached CSV. No record was created, changed, '
                      'or deleted by this report.')
        self.result_summary = '\n'.join(lines)

        # ── Detail CSV ─────────────────────────────────────────────────
        buf = io.StringIO()
        writer = csv.writer(buf)

        writer.writerow([f'=== CATEGORY CONFIGURATION vs. target account {self.target_account_code} ==='])
        writer.writerow(['Category', 'Cost Method', 'Valuation', 'Valuation Account', 'On Target Account?',
                          'Stock Variation Account', 'Stock Journal'])
        for r in categ_rows:
            writer.writerow([r['category'], r['cost_method'], r['valuation'],
                              r['valuation_account'], 'YES' if r['on_target'] else 'NO',
                              r['stock_variation_account'], r['stock_journal']])
        writer.writerow([])

        writer.writerow(['=== SITE GL ACCOUNT WIRING ==='])
        for label, value in site_account_rows:
            writer.writerow([label, value])
        writer.writerow([])

        if not has_file:
            writer.writerow(['=== NO MAIN SUMMARY WORKBOOK UPLOADED — item mapping, on-hand '
                              'cross-check and zero-value rows were skipped this run ==='])
            writer.writerow([])

        writer.writerow(['=== OPENING BALANCE ITEM MAPPING (non-zero value rows) ==='])
        writer.writerow(['Category', 'Description', 'Unit', 'Balance Qty', 'Balance Value',
                          'Match Status', 'Matched Product(s)'])
        for item in matched_items:
            matched_names = '; '.join(
                f"{p['name']} [{p['default_code'] or ''}]" for p in item['matched_products']
            )
            writer.writerow([item['category'], item['description'], item['unit'],
                              item['qty'], item['value'], item['match_status'], matched_names])
        writer.writerow([])

        if zero_value_with_qty:
            writer.writerow(['=== ZERO-VALUE ROWS WITH NON-ZERO QTY (FYI — not in the value total) ==='])
            writer.writerow(['Category', 'Description', 'Unit', 'Balance Qty'])
            for item in zero_value_with_qty:
                writer.writerow([item['category'], item['description'], item['unit'], item['qty']])
            writer.writerow([])

        writer.writerow(['=== ON-HAND CROSS-CHECK (exact single-match items) ==='])
        writer.writerow(['Category', 'Description', 'Sheet Qty', 'System On-Hand Qty', 'Diff'])
        for r in on_hand_rows:
            writer.writerow([r['category'], r['description'], r['sheet_qty'], r['system_qty'], r['diff']])
        writer.writerow([])

        writer.writerow([f'=== PURCHASE RECEIPTS AUDIT (since {self.receipts_since_date}, target account {self.target_account_code}) ==='])
        writer.writerow(['Picking', 'Date', 'Product', 'Category', 'Cost Method', 'Qty', 'Value',
                          'Currently Configured Account', 'Target Account', 'Accounts Actually on JE', 'Status'])
        for r in receipts_rows:
            writer.writerow([r['picking'], r['date'], r['product'], r['category'], r['cost_method'],
                              r['qty'], r['value'], r['configured_account'], r['target_account'],
                              r['actual_accounts_on_je'], r['status']])

        csv_bytes = buf.getvalue().encode('utf-8-sig')
        self.report_file = base64.b64encode(csv_bytes)
        self.report_filename = f'opening_balance_discovery_{site_config.name or site_config.id}.csv'

        return {
            'type': 'ir.actions.act_window',
            'res_model': self._name,
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'new',
        }
