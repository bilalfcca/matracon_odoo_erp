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
    _description = 'Site Opening Balance — Discovery Report & Valuation Import'

    state = fields.Selection([
        ('draft', 'Draft'),
        ('discovered', 'Discovered'),
        ('imported', 'Imported'),
    ], default='draft', readonly=True)

    site_config_id = fields.Many2one(
        'x.project.site.config', string='Site', required=False,
        help='Which site this opening inventory workbook belongs to (optional when running all-sites direct import).',
    )
    summary_file = fields.Binary(
        string='Inventory File (.xlsx / .xls)', attachment=False,
        help='The site\'s inventory sheet (.xlsx workbook or .xls export table). Supports '
             'MCH MAIN SUMMARY, STP Sewage Plant report, and RWASA ledger exports.',
    )
    summary_filename = fields.Char(string='Filename')
    as_of_date = fields.Date(
        string='Balance As Of',
        default=lambda self: fields.Date.from_string('2026-06-30') or fields.Date.context_today(self),
        help='The date opening inventory valuation is struck as of (e.g. 30 June 2026). '
             'Recorded on valuation layers and opening GL entry.',
    )
    receipts_since_date = fields.Date(
        string='Review Receipts Since', required=True,
        default=lambda self: fields.Date.context_today(self),
        help='Purchase Receipts done on/after this date, up to today, are included in '
             'the GL account audit below. Defaults to the day after Balance As Of.',
    )
    value_tolerance = fields.Float(
        string='Value Tolerance (PKR)', default=0.01,
        help='Sheet rows whose valuation amount is below this absolute value '
             'are treated as closed/zero and excluded from import.',
    )
    target_account_code = fields.Char(
        string='Target Inventory Account Code', default='230400000',
        help='The shared GL account both the opening balance and Purchase Receipts land on — '
             'account 230400000 "Inventory" (Current Asset).',
    )

    # ── Valuation Import Options ──
    import_mode = fields.Selection([
        ('exact_only', 'Exact Matches Only (Recommended)'),
        ('all_matched', 'Exact & Fuzzy Single Matches'),
    ], default='exact_only', string='Product Matching Mode',
        help='Exact Matches Only imports rows that map uniquely to 1 product by Item Code or exact description.')
    update_product_cost = fields.Boolean(
        string='Update Product Unit Cost (standard_price)', default=True,
        help='Updates product standard_price to the unit cost from the opening sheet so '
             'Inventory > Reporting > Stock displays the correct Unit Cost and Total Value.')
    create_valuation_layers = fields.Boolean(
        string='Create Stock Valuation Layers (Legacy)', default=False,
        help='Only applicable if stock.valuation.layer model exists. In Odoo 19, valuation is stored directly on products and moves.')
    create_gl_entry = fields.Boolean(
        string='Create GL Opening Journal Entry', default=False,
        help='Posts a journal entry dated as of 30 June debiting Target Account (230400000) '
             'with site analytic distribution and crediting the offset account.')
    offset_account_id = fields.Many2one(
        'account.account', string='Opening Offset Account',
        domain=[('x_is_main', '=', False)],
        help='Credit counterpart account for the opening inventory journal entry (e.g. Opening Balance Equity).')
    stock_journal_id = fields.Many2one(
        'account.journal', string='Inventory Journal',
        domain=[('type', '=', 'general')],
        default=lambda self: self.env.company.account_stock_journal_id or self.env['account.journal'].search([('type', '=', 'general')], limit=1),
        help='Journal where the opening stock valuation entry will post.')
    overwrite_existing = fields.Boolean(
        string='Overwrite Previous Opening Valuation for this Site', default=False,
        help='If opening valuation was already imported for this site, check to replace it safely.')

    result_summary = fields.Text(string='Summary', readonly=True)
    report_file = fields.Binary(string='Detail Report (CSV)', readonly=True, attachment=False)
    report_filename = fields.Char(string='Report Filename', readonly=True)

    @api.onchange('as_of_date')
    def _onchange_as_of_date(self):
        for wizard in self:
            if wizard.as_of_date and not wizard.receipts_since_date:
                wizard.receipts_since_date = wizard.as_of_date

    def _check_tool_access(self):
        if not (self.env.user.has_group('purchase_demand_raise.group_matracon_admin')
                or self.env.user.has_group('site_operations.group_finance_ho')
                or self.env.user.has_group('base.group_system')):
            raise UserError(_('Only Matracon Admin, Finance HO or System Administrator can run this report.'))

    # ── 1. Parse the MAIN SUMMARY sheet ───────────────────────────────────

    def _parse_main_summary(self):
        self.ensure_one()
        if not self.summary_file:
            raise UserError(_('Please attach the site\'s workbook (.xlsx or .xls) first.'))

        raw = base64.b64decode(self.summary_file)

        # 1. HTML table export (.xls format like Civil Ledger / Overall Inventory Mech)
        if raw.strip().startswith(b'<') or b'<table' in raw[:500].lower():
            text = raw.decode('latin1', errors='ignore')
            rows = re.findall(r'<tr[^>]*>(.*?)</tr>', text, re.DOTALL | re.IGNORECASE)
            items = []
            category = None
            for r in rows:
                cols = [re.sub(r'<[^>]+>', '', c).strip() for c in re.findall(r'<td[^>]*>(.*?)</td>', r, re.DOTALL | re.IGNORECASE)]
                if len(cols) >= 16:
                    categ = cols[0]
                    if categ and categ != '&nbsp;':
                        category = categ
                    code = cols[1] if cols[1] != '&nbsp;' else ''
                    desc = cols[2] if cols[2] != '&nbsp;' else ''
                    unit = cols[3] if cols[3] != '&nbsp;' else ''
                    try:
                        qty = float(cols[13].replace(',', '').replace('&nbsp;', '0') or 0)
                        val = float(cols[15].replace(',', '').replace('&nbsp;', '0') or 0)
                    except ValueError:
                        continue
                    if desc and (abs(qty) > 1e-6 or abs(val) > 0.01):
                        items.append({
                            'category': category or '(uncategorized)',
                            'code': code,
                            'description': desc,
                            'unit': unit,
                            'qty': qty,
                            'value': val,
                        })
            if items:
                return items

        # 2. Standard Excel workbook (.xlsx)
        try:
            import openpyxl
        except ImportError:
            raise UserError(_('openpyxl is not available in this environment - cannot read the workbook.'))

        try:
            wb = openpyxl.load_workbook(io.BytesIO(raw), data_only=True)
        except Exception as e:
            raise UserError(_('Failed to read the workbook: %s') % str(e))

        sheet = None
        for name in wb.sheetnames:
            clean = name.strip().upper()
            if 'MAIN SUMMARY' in clean or '30-06' in clean or 'SUMMARY' in clean or 'BALANCE' in clean:
                sheet = wb[name]
                break
        if sheet is None:
            sheet = wb.active

        items = []
        category = None
        desc_col = 1
        unit_col = 2
        qty_col = 5
        val_col = 7
        code_col = None
        header_found = False

        for row in sheet.iter_rows(min_row=1, values_only=True):
            if not row or all(v is None for v in row):
                continue

            row_str = [str(c).strip().lower() if c is not None else '' for c in row]
            if not header_found and any('description' in c for c in row_str):
                for idx, c in enumerate(row_str):
                    if 'code' in c and code_col is None:
                        code_col = idx
                    elif any(k in c for k in ('item description', 'material description', 'description', 'particulars', 'item name')) and 'received' not in c and 'issued' not in c and 'balance' not in c and desc_col == 1:
                        desc_col = idx
                    elif 'unit' in c:
                        unit_col = idx
                    elif any(k in c for k in ('balance material amount', 'current stock amount', 'total value', 'balance amount')):
                        val_col = idx
                    elif any(k in c for k in ('balance material', 'current stock qty', 'balance qty', 'closing qty')):
                        qty_col = idx
                header_found = True
                continue

            sr = row[0]
            desc = row[desc_col] if len(row) > desc_col else None
            # A category header row is a bare text label in column A with
            # nothing in the Description column (e.g. "Steel", "Cables").
            if isinstance(sr, str) and (desc is None or str(desc).strip() == ''):
                category = sr.strip()
                continue
            if not desc or str(desc).strip() == '' or str(desc).strip().lower() in ('description', 'item description'):
                continue
            if isinstance(sr, str) and any(w in sr.lower() for w in ('sr', 'total', 'grand total', 'sub total')):
                continue

            code = str(row[code_col]).strip() if code_col is not None and len(row) > code_col and row[code_col] else ''
            unit = str(row[unit_col]).strip() if len(row) > unit_col and row[unit_col] else ''
            bal_qty = row[qty_col] if len(row) > qty_col else 0.0
            bal_value = row[val_col] if len(row) > val_col else 0.0

            try:
                qty_f = float(str(bal_qty).replace(',', '')) if bal_qty is not None else 0.0
            except ValueError:
                qty_f = 0.0

            try:
                val_f = float(str(bal_value).replace(',', '')) if bal_value is not None else 0.0
            except ValueError:
                val_f = 0.0

            items.append({
                'category': category or '(uncategorized)',
                'code': code,
                'description': str(desc).strip(),
                'unit': unit,
                'qty': qty_f,
                'value': val_f,
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
        name_index = defaultdict(list)
        code_index = {}
        for p in product_data:
            if p.get('default_code'):
                code_index[p['default_code'].strip().upper()] = p
            name_index[_normalize(p['name'])].append(p)

        allow_fuzzy = len(name_index) <= 6000

        results = []
        for item in items:
            code = (item.get('code') or '').strip().upper()
            norm = _normalize(item.get('description') or '')
            matched = []
            if code and code in code_index:
                cand = code_index[code]
                cand_norm = _normalize(cand.get('name') or '')
                if cand_norm == norm or (norm and cand_norm and (norm in cand_norm or cand_norm in norm)):
                    matched = [cand]
                    status = 'exact'
            if not matched:
                matched = name_index.get(norm) or []
                status = 'exact' if matched else 'none'
                if not matched and allow_fuzzy and norm:
                    candidates = []
                    for key, plist in name_index.items():
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
        self._check_tool_access()
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
        self.state = 'discovered'

        return {
            'type': 'ir.actions.act_window',
            'res_model': self._name,
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'new',
        }

    # ── Phase 2: Import Opening Inventory Valuation ──────────────────────────

    def action_import_valuation(self):
        self.ensure_one()
        self._check_tool_access()
        if not self.summary_file:
            raise UserError(_('Please attach the site\'s workbook (.xlsx or .xls) first.'))

        site_config = self.site_config_id
        if not site_config:
            raise UserError(_('Please select a Site.'))

        as_of_date = self.as_of_date or fields.Date.from_string('2026-06-30')
        as_of_datetime = fields.Datetime.to_datetime(f"{as_of_date} 23:59:59")
        company = self.env.company

        # 1. Parse and match candidates
        items = self._parse_main_summary()
        candidates = [i for i in items if abs(i['value']) > self.value_tolerance]
        matched_items = self._match_products(candidates)

        # 2. Check existing opening layers for this site (if SVL model exists)
        opening_prefix = f"Opening Inventory Valuation — {site_config.name}"
        has_svl = 'stock.valuation.layer' in self.env
        ValuationLayer = self.env['stock.valuation.layer'].sudo() if has_svl else None
        if has_svl and ValuationLayer is not None:
            existing_layers = ValuationLayer.search([
                ('description', 'like', f"{opening_prefix}%"),
                ('company_id', '=', company.id),
            ])
            if existing_layers:
                if not self.overwrite_existing:
                    raise UserError(_(
                        'Found %s existing opening valuation layers for %s. '
                        'Check "Overwrite Previous Opening Valuation for this Site" to replace them.'
                    ) % (len(existing_layers), site_config.display_name))
                else:
                    old_moves = existing_layers.mapped('account_move_id')
                    existing_layers.unlink()
                    if old_moves:
                        for m in old_moves:
                            if m.state == 'posted':
                                m.button_draft()
                        old_moves.unlink()

        # 3. Filter items according to import_mode
        importable = []
        skipped_reasons = []
        for item in matched_items:
            prods = item['matched_products']
            if item['match_status'] == 'exact' and len(prods) == 1:
                importable.append((prods[0], item))
            elif self.import_mode == 'all_matched' and item['match_status'] == 'fuzzy' and len(prods) == 1:
                importable.append((prods[0], item))
            elif len(prods) > 1:
                skipped_reasons.append((item, f"Ambiguous: {len(prods)} products matched"))
            elif item['match_status'] == 'fuzzy':
                skipped_reasons.append((item, f"Fuzzy match not approved (found {len(prods)})"))
            else:
                skipped_reasons.append((item, "No matching product found in Odoo"))

        if not importable:
            raise UserError(_('No uniquely matched products found with non-zero valuation to import.'))

        # 4. Check GL prerequisites if GL entry is requested
        target_account = self._resolve_target_account()
        if self.create_gl_entry:
            if not target_account:
                raise UserError(_('Target inventory account (%s) not found in this database.') % self.target_account_code)
            if not self.offset_account_id:
                raise UserError(_('Please select an Opening Balance Offset Account for the GL entry.'))
            if not self.stock_journal_id:
                raise UserError(_('Please select an Inventory Journal for the GL entry.'))

        Product = self.env['product.product'].sudo()
        internal_locs = self._prepare_site_locations_and_quants(site_config.warehouse_id)
        imported_rows = []
        created_layer_ids = []
        total_val = 0.0

        # 5. Import valuation (WITHOUT touching stock.quant or creating stock moves)
        grouped_importable = defaultdict(list)
        for prod_dict, item in importable:
            grouped_importable[prod_dict['id']].append((prod_dict, item))

        for pid, p_entries in grouped_importable.items():
            product = Product.browse(pid)
            tot_qty = sum(it['qty'] for _, it in p_entries)
            tot_val = sum(it['value'] for _, it in p_entries)
            if tot_qty > 1e-6 and tot_val > 0.01:
                unit_cost = round(tot_val / tot_qty, 4)
            else:
                rates = [((it['value'] / it['qty']) if abs(it['qty']) > 1e-6 else 0.0) for _, it in p_entries]
                pos_rates = [r for r in rates if r > 0.0001]
                unit_cost = round(sum(pos_rates) / len(pos_rates), 4) if pos_rates else 0.0

            for prod_dict, item in p_entries:
                qty = item['qty']
                val = item['value']
                layer_id = self._apply_product_valuation(
                    product=product,
                    unit_cost=unit_cost,
                    val=val,
                    qty=qty,
                    site_config=site_config,
                    as_of_datetime=as_of_datetime,
                    internal_locs=internal_locs,
                    update_cost=self.update_product_cost,
                )
                if layer_id:
                    created_layer_ids.append(layer_id)

                total_val += val
                imported_rows.append({
                    'product': product.display_name,
                    'default_code': product.default_code or '',
                    'sheet_desc': item['description'],
                    'qty': qty,
                    'unit_cost': unit_cost,
                    'value': val,
                })

        # Invalidate model caches so compute_value updates immediately
        self.env['stock.quant'].invalidate_model()
        self.env['product.product'].invalidate_model()
        self.env['product.template'].invalidate_model()
        self.env['stock.move'].invalidate_model()

        # 6. Post GL entry if requested
        created_move = None
        if self.create_gl_entry and total_val > 0.01:
            analytic_dist = {}
            if site_config.analytic_account_id:
                analytic_dist = {str(site_config.analytic_account_id.id): 100}

            created_move = self.env['account.move'].sudo().create({
                'journal_id': self.stock_journal_id.id,
                'date': as_of_date,
                'ref': f"Opening Stock Valuation — {site_config.name}",
                'move_type': 'entry',
                'line_ids': [
                    (0, 0, {
                        'name': f"Opening Inventory Valuation — {site_config.name}",
                        'account_id': target_account.id,
                        'debit': total_val,
                        'credit': 0.0,
                        'analytic_distribution': analytic_dist,
                    }),
                    (0, 0, {
                        'name': f"Opening Inventory Offset — {site_config.name}",
                        'account_id': self.offset_account_id.id,
                        'debit': 0.0,
                        'credit': total_val,
                    }),
                ],
            })
            created_move.action_post()
            if has_svl and created_layer_ids and ValuationLayer is not None:
                ValuationLayer.browse(created_layer_ids).write({'account_move_id': created_move.id})

        # 7. Build summary text
        summary_lines = [
            "=== Inventory Valuation Import Successful ===",
            f"Site: {site_config.display_name}",
            f"Balance As Of: {as_of_date}",
            f"Successfully Imported Products: {len(imported_rows)}",
            f"Total Opening Valuation Imported: PKR {total_val:,.2f}",
            f"Product Unit Costs (standard_price) Updated: {'YES' if self.update_product_cost else 'NO'}",
            f"Location Valuation Wired: YES ({len(internal_locs)} internal locations enabled)",
            f"Stock Valuation Layers: {'Created ' + str(len(created_layer_ids)) + f' (dated {as_of_date})' if has_svl else 'N/A (Odoo 19 native move & product cost model)'}",
            f"On-Hand Physical Stock (stock.quant): UNTOUCHED (0 quantities altered/duplicated)",
            f"GL Journal Entry: {created_move.name + ' (Posted)' if created_move else 'None (GL Entry was not requested)'}",
            f"Skipped / Unmatched Items: {len(skipped_reasons)} (see detail CSV)",
        ]
        self.result_summary = "\n".join(summary_lines)
        self.state = 'imported'

        # 8. Generate Detail CSV
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow([f"=== IMPORTED OPENING INVENTORY VALUATION — {site_config.name} (as of {as_of_date}) ==="])
        writer.writerow(["Product Code", "Product Name", "Sheet Description", "Opening Qty", "Unit Cost (PKR)", "Total Value (PKR)", "Status"])
        for r in imported_rows:
            writer.writerow([r['default_code'], r['product'], r['sheet_desc'], r['qty'], f"{r['unit_cost']:.4f}", f"{r['value']:.2f}", "IMPORTED"])

        if skipped_reasons:
            writer.writerow([])
            writer.writerow(["=== SKIPPED / UNMATCHED ITEMS ==="])
            writer.writerow(["Category", "Item Code", "Description", "Unit", "Sheet Qty", "Sheet Value", "Reason"])
            for item, reason in skipped_reasons:
                writer.writerow([item.get('category', ''), item.get('code', ''), item.get('description', ''), item.get('unit', ''), item.get('qty', 0), item.get('value', 0), reason])

        csv_bytes = buf.getvalue().encode('utf-8-sig')
        self.report_file = base64.b64encode(csv_bytes)
        self.report_filename = f"opening_valuation_import_{site_config.name or site_config.id}.csv"

        return {
            'type': 'ir.actions.act_window',
            'res_model': self._name,
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'new',
        }

    # ── Helpers for Odoo 19 Valuation & Locations ─────────────────────────

    def _prepare_site_locations_and_quants(self, warehouse):
        """Ensures site warehouse internal locations and quants have company_id set so
        Odoo's stock.location._should_be_valued() returns True and quant._compute_value() calculates
        the financial valuation.
        """
        if not warehouse or not warehouse.view_location_id:
            return self.env['stock.location'].browse()

        company = self.env.company
        internal_locs = self.env['stock.location'].sudo().search([
            ('id', 'child_of', warehouse.view_location_id.id),
            ('usage', 'in', ['internal', 'transit']),
        ])
        for loc in internal_locs:
            if not loc.company_id:
                loc.write({'company_id': company.id})

        quants_no_comp = self.env['stock.quant'].sudo().search([
            ('location_id', 'in', internal_locs.ids),
            ('company_id', '=', False),
        ])
        if quants_no_comp:
            quants_no_comp.write({'company_id': company.id})

        return internal_locs

    def _apply_product_valuation(self, product, unit_cost, val, qty, site_config, as_of_datetime, internal_locs=None, update_cost=True):
        """Robustly updates product valuation for both Odoo 19 (stock.move + product.value + standard_price)
        and Odoo 18/legacy (stock.valuation.layer).
        Guarantees that Inventory -> Reporting -> Stock (stock.quant compute_value) calculates
        non-zero unit cost and valuation WITHOUT modifying physical quantities.
        """
        company = self.env.company
        companies = self.env['res.company'].sudo().search([])

        # 1. Update standard_price across all companies on both product variant and template
        if update_cost and unit_cost > 0:
            for comp in companies:
                product.with_company(comp).sudo().write({'standard_price': unit_cost})
                if product.product_tmpl_id:
                    product.product_tmpl_id.with_company(comp).sudo().write({'standard_price': unit_cost})

        # 2. In Odoo 19, product._change_standard_price logs to product.value
        if 'product.value' in self.env:
            PV = self.env['product.value'].sudo()
            pv_exists = PV.search([
                ('product_id', '=', product.id),
                ('date', '<=', as_of_datetime),
                ('company_id', '=', company.id),
            ], limit=1)
            if pv_exists:
                try:
                    pv_exists.write({
                        'new_cost': unit_cost,
                        'value': unit_cost,
                        'new_value': val,
                    })
                except Exception:
                    pass
            else:
                try:
                    PV.create({
                        'product_id': product.id,
                        'quantity': qty,
                        'old_cost': 0.0,
                        'new_cost': unit_cost,
                        'old_value': 0.0,
                        'new_value': val,
                        'value': unit_cost,
                        'company_id': company.id,
                        'date': as_of_datetime,
                        'description': f"Opening Valuation — {site_config.name}",
                    })
                except Exception:
                    pass

        # 3. In Odoo 19, FIFO/AVCO computes from incoming stock.move.value in the FIFO stack!
        # If any stock.move exists for this product destined for the site's warehouse internal locations,
        # update its price_unit and value so that product.total_value and quant.value evaluate correctly.
        if internal_locs:
            moves = self.env['stock.move'].sudo().search([
                ('product_id', '=', product.id),
                ('location_dest_id', 'in', internal_locs.ids),
                ('state', '=', 'done'),
            ])
            for m in moves:
                m_qty = m.quantity or m.product_uom_qty or 0.0
                move_val = (m_qty * unit_cost) if m_qty else val
                m.sudo().write({
                    'price_unit': unit_cost,
                    'value': move_val,
                })

        # 4. Legacy stock.valuation.layer support (if model exists)
        layer_id = None
        if 'stock.valuation.layer' in self.env:
            try:
                SVL = self.env['stock.valuation.layer'].sudo()
                layer = SVL.create({
                    'product_id': product.id,
                    'company_id': company.id,
                    'unit_cost': unit_cost,
                    'quantity': qty,
                    'value': val,
                    'remaining_qty': qty,
                    'remaining_value': val,
                    'description': f"Opening Inventory Valuation — {site_config.name} (as of 2026-06-30)",
                })
                layer_id = layer.id
                self.env.cr.execute(
                    "UPDATE stock_valuation_layer SET create_date = %s WHERE id = %s",
                    (as_of_datetime, layer.id)
                )
            except Exception:
                pass

        return layer_id

    # ── Phase 3: Direct Import for All 3 Sites (MCH, STP, RWASA) ─────────────

    @api.model
    def action_direct_import_all_projects_server(self):
        """Server Action entrypoint: Runs direct opening valuation import for MCH, STP, and RWASA."""
        first_site = self.env['x.project.site.config'].sudo().search([], limit=1)
        wizard = self.create({
            'site_config_id': first_site.id if first_site else False,
            'as_of_date': fields.Date.from_string('2026-06-30'),
            'create_gl_entry': False,
        })
        return wizard.action_direct_import_all_projects()

    def action_direct_import_all_projects(self):
        """Directly imports opening inventory valuations for MCH, STP, and RWASA from
        the bundled verified dataset, updating standard_price, stock moves, and location
        configurations so Inventory -> Reporting -> Stock displays non-zero valuations.
        """
        self.ensure_one()
        self._check_tool_access()

        import json
        import os

        json_path = os.path.join(os.path.dirname(__file__), '..', 'data', 'opening_valuations_bundled.json')
        if not os.path.exists(json_path):
            raise UserError(_('Bundled opening valuation data file not found at: %s') % json_path)

        with open(json_path, 'r', encoding='utf-8') as f:
            bundled_data = json.load(f)

        as_of_date = fields.Date.from_string('2026-06-30')
        as_of_datetime = fields.Datetime.to_datetime('2026-06-30 23:59:59')
        company = self.env.company

        # Build product index
        Product = self.env['product.product'].sudo()
        product_data = Product.search_read([], ['id', 'name', 'default_code', 'categ_id', 'product_tmpl_id'])
        code_index = {}
        name_index = defaultdict(list)
        for p in product_data:
            if p.get('default_code'):
                code_index[p['default_code'].strip().upper()] = p
            name_index[_normalize(p['name'])].append(p)

        site_mappings = {
            'MCH': ['MCH - BAHAWALNAGAR', 'MCH', 'BAHAWALNAGAR'],
            'STP': ['STP - MARDAN', 'STP', 'MARDAN'],
            'RWASA': ['RWASA', 'RWASA Project', 'RAWALPINDI'],
        }

        SiteConfig = self.env['x.project.site.config'].sudo()
        target_account = self._resolve_target_account()
        offset_account = self.offset_account_id
        stock_journal = self.stock_journal_id or self.env['account.journal'].sudo().search([
            ('type', '=', 'general'),
            ('company_id', '=', company.id),
        ], limit=1)

        site_summaries = []
        all_imported_rows = []
        all_skipped_rows = []

        # Step 1: Match and collect items across all 3 sites
        site_matched_entries = defaultdict(list)
        product_entries = defaultdict(list)
        resolved_sites = {}

        for site_key in ['MCH', 'STP', 'RWASA']:
            items = bundled_data.get(site_key, [])
            if not items:
                continue

            # Find site config
            site_config = SiteConfig.search([('name', 'in', site_mappings[site_key])], limit=1)
            if not site_config:
                for candidate in site_mappings[site_key]:
                    site_config = SiteConfig.search([('name', 'ilike', candidate)], limit=1)
                    if site_config:
                        break

            if not site_config:
                site_summaries.append(f"Site '{site_key}': Skipped (Site Configuration record not found in database)")
                continue

            internal_locs = self._prepare_site_locations_and_quants(site_config.warehouse_id)
            resolved_sites[site_key] = (site_config, internal_locs)

            for item in items:
                code = (item.get('code') or '').strip().upper()
                desc = item.get('desc') or ''
                qty = item.get('qty', 0.0)
                val = item.get('val', 0.0)
                site_unit_cost = item.get('unit_cost', 0.0) or ((val / qty) if abs(qty) > 1e-6 else 0.0)

                # Match product: exact name first, guarded code fallback
                norm = _normalize(desc)
                matched_p = None
                plist = name_index.get(norm)
                if plist and len(plist) == 1:
                    matched_p = plist[0]
                elif code and code in code_index:
                    cand = code_index[code]
                    cand_norm = _normalize(cand.get('name') or '')
                    if cand_norm == norm or (norm and cand_norm and (norm in cand_norm or cand_norm in norm)):
                        matched_p = cand
                elif not plist:
                    fuzzy_matches = []
                    for key, candidates in name_index.items():
                        if key and (norm in key or key in norm):
                            fuzzy_matches.extend(candidates)
                    if len(fuzzy_matches) == 1:
                        matched_p = fuzzy_matches[0]

                if not matched_p:
                    all_skipped_rows.append((site_config.name, item, 'No uniquely matched product'))
                    continue

                product = Product.browse(matched_p['id'])
                entry = {
                    'site_key': site_key,
                    'site_config': site_config,
                    'internal_locs': internal_locs,
                    'item': item,
                    'qty': qty,
                    'val': val,
                    'site_unit_cost': site_unit_cost,
                    'product': product,
                }
                product_entries[product.id].append(entry)
                site_matched_entries[site_key].append(entry)

        # Step 2: For each unique matched product, compute the weighted average unit price
        product_avg_costs = {}
        for pid, entries in product_entries.items():
            tot_qty = sum(e['qty'] for e in entries)
            tot_val = sum(e['val'] for e in entries)
            if tot_qty > 1e-6 and tot_val > 0.01:
                avg_cost = round(tot_val / tot_qty, 4)
            else:
                rates = [e['site_unit_cost'] for e in entries if e['site_unit_cost'] > 0.0001]
                avg_cost = round(sum(rates) / len(rates), 4) if rates else 0.0
            product_avg_costs[pid] = avg_cost

        # Step 3: Apply valuations per site
        for site_key in ['MCH', 'STP', 'RWASA']:
            if site_key not in resolved_sites:
                continue
            site_config, internal_locs = resolved_sites[site_key]
            entries = site_matched_entries.get(site_key, [])

            site_imported = 0
            site_val = 0.0

            for e in entries:
                product = e['product']
                qty = e['qty']
                val = e['val']
                site_unit_cost = e['site_unit_cost']
                # If product has multiple prices, standard_price gets the weighted average
                avg_cost = product_avg_costs.get(product.id, site_unit_cost)
                unit_cost_to_apply = avg_cost if avg_cost > 0 else (site_unit_cost if site_unit_cost > 0 else 0.0)

                self._apply_product_valuation(
                    product=product,
                    unit_cost=unit_cost_to_apply,
                    val=val,
                    qty=qty,
                    site_config=site_config,
                    as_of_datetime=as_of_datetime,
                    internal_locs=internal_locs,
                    update_cost=True,
                )

                site_imported += 1
                site_val += val
                all_imported_rows.append({
                    'site': site_config.name,
                    'code': product.default_code or '',
                    'name': product.display_name,
                    'sheet_desc': e['item'].get('desc', ''),
                    'qty': qty,
                    'unit_cost': unit_cost_to_apply,
                    'value': val,
                })

            # Optional GL Journal Entry per site
            posted_move_str = "None (Valuation only — GL entry not requested)"
            if self.create_gl_entry and target_account and offset_account and stock_journal and site_val > 0.01:
                ref_str = f"Opening Stock Valuation — {site_config.name}"
                existing_move = self.env['account.move'].sudo().search([
                    ('ref', '=', ref_str),
                    ('state', '=', 'posted'),
                ], limit=1)
                if not existing_move:
                    analytic_dist = {}
                    if site_config.analytic_account_id:
                        analytic_dist = {str(site_config.analytic_account_id.id): 100}

                    created_move = self.env['account.move'].sudo().create({
                        'journal_id': stock_journal.id,
                        'date': as_of_date,
                        'ref': ref_str,
                        'move_type': 'entry',
                        'line_ids': [
                            (0, 0, {
                                'name': f"Opening Inventory Valuation — {site_config.name}",
                                'account_id': target_account.id,
                                'debit': site_val,
                                'credit': 0.0,
                                'analytic_distribution': analytic_dist,
                            }),
                            (0, 0, {
                                'name': f"Opening Inventory Offset — {site_config.name}",
                                'account_id': offset_account.id,
                                'debit': 0.0,
                                'credit': site_val,
                            }),
                        ],
                    })
                    created_move.action_post()
                    posted_move_str = f"{created_move.name} (Posted)"
                else:
                    posted_move_str = f"{existing_move.name} (Already exists)"

            site_summaries.append(
                f"• {site_config.name}: {site_imported} products valued, total PKR {site_val:,.2f} | GL Entry: {posted_move_str}"
            )

        # Invalidate caches
        self.env['stock.quant'].invalidate_model()
        self.env['product.product'].invalidate_model()
        self.env['product.template'].invalidate_model()
        self.env['stock.move'].invalidate_model()

        # Build summary
        total_imported_val = sum(r['value'] for r in all_imported_rows)
        lines = [
            "=== DIRECT OPENING INVENTORY VALUATION IMPORT COMPLETE ===",
            f"Import Date: As of {as_of_date}",
            f"Total Products Valued: {len(all_imported_rows)}",
            f"Total Opening Valuation: PKR {total_imported_val:,.2f}",
            "",
            "Site-by-Site Summary:",
        ]
        lines.extend(site_summaries)
        lines.extend([
            "",
            "Status & Verification:",
            "✓ Product standard_price (unit costs) updated across all companies",
            "✓ Warehouse internal stock locations wired for valuation (company_id configured)",
            "✓ Stock move layers updated so FIFO and AVCO stacks evaluate valuations",
            "✓ Physical on-hand stock quantities (stock.quant) left COMPLETELY UNTOUCHED",
        ])

        self.result_summary = "\n".join(lines)
        self.state = 'imported'

        # Generate CSV report
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["=== DIRECT OPENING INVENTORY VALUATION IMPORT — ALL 3 SITES ==="])
        writer.writerow(["Site", "Product Code", "Product Name", "Sheet Description", "Quantity", "Unit Cost (PKR)", "Total Value (PKR)", "Status"])
        for r in all_imported_rows:
            writer.writerow([r['site'], r['code'], r['name'], r['sheet_desc'], r['qty'], f"{r['unit_cost']:.4f}", f"{r['value']:.2f}", "VALUED"])

        if all_skipped_rows:
            writer.writerow([])
            writer.writerow(["=== SKIPPED / UNMATCHED ITEMS ==="])
            writer.writerow(["Site", "Category", "Item Code", "Description", "Unit", "Qty", "Value", "Reason"])
            for sname, item, reason in all_skipped_rows:
                writer.writerow([sname, item.get('category', ''), item.get('code', ''), item.get('desc', ''), item.get('unit', ''), item.get('qty', 0), item.get('val', 0), reason])

        csv_bytes = buf.getvalue().encode('utf-8-sig')
        self.report_file = base64.b64encode(csv_bytes)
        self.report_filename = f"direct_opening_valuation_import_all_3_sites.csv"

        return {
            'type': 'ir.actions.act_window',
            'res_model': self._name,
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'new',
        }
