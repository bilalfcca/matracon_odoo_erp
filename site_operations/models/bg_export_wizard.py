"""
Bank Guarantee Registry — Excel Export Wizard

Generates a formatted XLSX file with optional grouping by Bank and/or Project.
Each grouping level gets a section header row and a subtotal row.
Both groupings together produce a Bank → Project two-level hierarchy.

Optional column groups let users append Project Timeline and/or Project
Financial columns to the standard registry columns.
"""
import base64
import datetime as dt
import logging
from collections import defaultdict
from io import BytesIO

import xlsxwriter

from odoo import models, fields, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

# ── Base columns (always exported) ─────────────────────────────────────────────
# (column_header, bg_field_path, col_width_chars, fmt_type)
# fmt_type: 'text' | 'num' | 'pct' | 'date'
# bg_field_path: dot-separated path resolved via _get(); direct attr for related fields
_BASE_COLUMNS = [
    ('Sr No',              'sr_no',               10, 'text'),
    ('Guarantee No',       'guarantee_number',     18, 'text'),
    ('Nature',             'nature_id.name',       15, 'text'),
    ('Bank',               'journal_id.name',      20, 'text'),
    ('Project',            'project_id.name',      22, 'text'),
    ('Beneficiary',        'beneficiary_name',     24, 'text'),
    ('Type',               'jv_type',              10, 'text'),
    ('Issue Date',         'issue_date',           12, 'date'),
    ('Expiry Date',        'expiry_date',          12, 'date'),
    ('BG Amount',          'bg_amount',            16, 'num'),
    ('Cash Margin %',      'cash_margin_percent',  12, 'pct'),
    ('Cash Margin Amt',    'margin_amount',        18, 'num'),
    ('Commission %',       'pricing_percent',      12, 'pct'),
    ('Commission Amt',     'commission_amount',    18, 'num'),
    ('FED %',              'fed_percent',           8, 'pct'),
    ('FED Amount',         'fed_amount',           14, 'num'),
    ('Status',             'state',                12, 'text'),
]

# ── Optional: Project Timeline (from Site Project Configuration) ────────────────
_OPT_TIMELINE = [
    ('Project Start Date',   'x_proj_start_date',          14, 'date'),
    ('Original Deadline',    'x_proj_original_deadline',   14, 'date'),
    ('Current Deadline',     'x_proj_current_deadline',    14, 'date'),
]

# ── Optional: Project Financials (from linked Odoo project) ────────────────────
# Note: 'num' columns are included in group subtotals; 'pct' columns are not.
_OPT_FINANCIALS = [
    ('Contract Value',        'x_proj_contract_value',           18, 'num'),
    ('Billed to Client',      'x_proj_billed_to_client',         18, 'num'),
    ('Work Completion %',     'x_proj_work_completion_pct',      14, 'pct'),
    ('Financial Cmpl %',      'x_proj_financial_completion_pct', 16, 'pct'),
    ('Remaining to Bill',     'x_proj_remaining_to_bill',        18, 'num'),
]

# Index of the first numeric (summable) column in the base set — used for
# label-merge width in subtotal rows.  bg_amount is always at position 9.
_FIRST_AMT_FIELD = 'bg_amount'


class BgExportWizard(models.TransientModel):
    _name = 'x.bg.export.wizard'
    _description = 'Bank Guarantee Registry Export'

    group_by_bank = fields.Boolean('Group by Bank', default=True)
    group_by_project = fields.Boolean('Group by Project', default=False)
    include_released = fields.Boolean(
        'Include Released / Expired / Cancelled', default=False,
        help='When unchecked only Draft, Pending, Active, and Locked BGs are exported.')

    col_project_timeline = fields.Boolean(
        'Project Timeline', default=False,
        help='Appends three date columns: Project Start Date, Original Deadline '
             '(Baseline), and Current Deadline (after EOTs).')
    col_project_financials = fields.Boolean(
        'Project Financials', default=False,
        help='Appends five columns: Contract Value, Billed to Client, '
             'Work Completion %, Financial Completion %, and Remaining to Bill. '
             'Monetary columns are included in group subtotals.')

    def _build_columns(self):
        """Return the final ordered column list based on wizard options."""
        cols = list(_BASE_COLUMNS)
        if self.col_project_timeline:
            cols += _OPT_TIMELINE
        if self.col_project_financials:
            cols += _OPT_FINANCIALS
        return cols

    def action_export(self):
        self.ensure_one()

        # ── Dynamic column metadata ────────────────────────────────────────
        columns = self._build_columns()
        ncols = len(columns)
        field_idx = {col[1]: i for i, col in enumerate(columns)}
        # Columns to sum in subtotal rows (all 'num' type)
        amt_cols = [i for i, (_, _, _, ft) in enumerate(columns) if ft == 'num']
        # Merge label from col 0 → first_amt-1 in subtotal rows
        first_amt = field_idx.get(_FIRST_AMT_FIELD, 9)

        # ── Data ──────────────────────────────────────────────────────────
        domain = []
        if not self.include_released:
            domain += [('state', 'not in', ('released', 'expired', 'cancelled'))]
        bgs = self.env['x.bank.guarantee'].sudo().search(
            domain, order='journal_id, project_id, issue_date, id')

        if not bgs:
            raise UserError(_('No Bank Guarantees found matching the selected filters.'))

        buf = BytesIO()
        wb = xlsxwriter.Workbook(buf, {'in_memory': True, 'remove_timezone': True})
        ws = wb.add_worksheet('BG Registry')

        # ── Formats ───────────────────────────────────────────────────────
        NAVY = '#003366'
        f = {
            'title':      wb.add_format({'bold': True, 'font_size': 13, 'align': 'center',
                                          'valign': 'vcenter', 'bg_color': NAVY,
                                          'font_color': 'white', 'border': 1}),
            'header':     wb.add_format({'bold': True, 'font_size': 10, 'align': 'center',
                                          'valign': 'vcenter', 'bg_color': NAVY,
                                          'font_color': 'white', 'border': 1, 'text_wrap': True}),
            'header_opt': wb.add_format({'bold': True, 'font_size': 10, 'align': 'center',
                                          'valign': 'vcenter', 'bg_color': '#1a5276',
                                          'font_color': 'white', 'border': 1, 'text_wrap': True,
                                          'italic': True}),
            'group':      wb.add_format({'bold': True, 'bg_color': '#C6DEFF', 'border': 1,
                                          'font_size': 10, 'valign': 'vcenter'}),
            'subgroup':   wb.add_format({'bold': True, 'bg_color': '#DDEEFF', 'border': 1,
                                          'font_size': 10, 'valign': 'vcenter', 'indent': 1}),
            'text':       wb.add_format({'border': 1, 'font_size': 9, 'valign': 'vcenter'}),
            'num':        wb.add_format({'border': 1, 'font_size': 9, 'num_format': '#,##0',
                                          'valign': 'vcenter'}),
            'pct':        wb.add_format({'border': 1, 'font_size': 9, 'num_format': '0.00"%"',
                                          'valign': 'vcenter'}),
            'date':       wb.add_format({'border': 1, 'font_size': 9, 'num_format': 'dd/mm/yyyy',
                                          'valign': 'vcenter'}),
            'sub_lbl':    wb.add_format({'bold': True, 'bg_color': '#C8F0DA', 'border': 1,
                                          'font_size': 9, 'align': 'right', 'valign': 'vcenter'}),
            'sub_num':    wb.add_format({'bold': True, 'bg_color': '#C8F0DA', 'border': 1,
                                          'font_size': 9, 'num_format': '#,##0', 'valign': 'vcenter'}),
            'grand_lbl':  wb.add_format({'bold': True, 'bg_color': '#FFE5B4', 'border': 1,
                                          'font_size': 10, 'align': 'right', 'valign': 'vcenter'}),
            'grand_num':  wb.add_format({'bold': True, 'bg_color': '#FFE5B4', 'border': 1,
                                          'font_size': 10, 'num_format': '#,##0', 'valign': 'vcenter'}),
        }

        ws.freeze_panes(2, 0)

        # Column widths
        for c, (_, _, w, _) in enumerate(columns):
            ws.set_column(c, c, w)

        # Row 0: title
        ws.merge_range(0, 0, 0, ncols - 1, 'Bank Guarantee Registry', f['title'])
        ws.set_row(0, 22)

        # Row 1: headers — optional columns use a slightly different shade to distinguish
        n_base = len(_BASE_COLUMNS)
        for c, (label, _, _, _) in enumerate(columns):
            hdr_fmt = f['header_opt'] if c >= n_base else f['header']
            ws.write(1, c, label, hdr_fmt)
        ws.set_row(1, 30)

        row = 2

        # ── Field value helpers ────────────────────────────────────────────
        state_map = dict(self.env['x.bank.guarantee']._fields['state'].selection)
        jv_map = dict(self.env['x.bank.guarantee']._fields['jv_type'].selection)

        def _get(bg, field):
            """Resolve a dot-separated field path on a bg record."""
            if field == 'state':
                return state_map.get(bg.state, bg.state)
            if field == 'jv_type':
                return jv_map.get(bg.jv_type, bg.jv_type)
            v = bg
            for part in field.split('.'):
                v = getattr(v, part, False)
                if not v and v != 0:
                    return False
            return v

        def _write_data_row(r, bg):
            for c, (_, field, _, ftype) in enumerate(columns):
                v = _get(bg, field)
                if ftype == 'date':
                    if v and isinstance(v, dt.date):
                        ws.write_datetime(r, c, dt.datetime.combine(v, dt.time()), f['date'])
                    else:
                        ws.write(r, c, '', f['text'])
                elif ftype == 'num':
                    ws.write_number(r, c, float(v or 0), f['num'])
                elif ftype == 'pct':
                    ws.write_number(r, c, float(v or 0), f['pct'])
                else:
                    ws.write(r, c, str(v) if v else '', f['text'])
            return r + 1

        def _totals(recs):
            """Sum all 'num' columns across recs; keyed by column index."""
            result = defaultdict(float)
            for bg in recs:
                for i, (_, field, _, ftype) in enumerate(columns):
                    if ftype == 'num':
                        result[i] += float(_get(bg, field) or 0)
            return dict(result)

        def _write_subtotal(r, label, totals, lbl_fmt, num_fmt):
            """Merge cols 0..first_amt-1 for label; write totals in num cols; blank elsewhere."""
            ws.merge_range(r, 0, r, first_amt - 1, label, lbl_fmt)
            for c in range(first_amt, ncols):
                if c in amt_cols:
                    ws.write_number(r, c, totals.get(c, 0.0), num_fmt)
                else:
                    ws.write(r, c, '', lbl_fmt)
            return r + 1

        # ── Grouping ───────────────────────────────────────────────────────

        if self.group_by_bank and self.group_by_project:
            banks = defaultdict(lambda: defaultdict(list))
            for bg in bgs:
                banks[bg.journal_id][bg.project_id].append(bg)

            grand = defaultdict(float)
            for bank in sorted(banks, key=lambda b: b.name or ''):
                ws.merge_range(row, 0, row, ncols - 1, f'Bank: {bank.name}', f['group'])
                row += 1
                bank_t = defaultdict(float)
                for project in sorted(banks[bank], key=lambda p: p.name or ''):
                    ws.merge_range(row, 0, row, ncols - 1,
                                   f'  Project: {project.name or "(No Project)"}', f['subgroup'])
                    row += 1
                    recs = banks[bank][project]
                    for bg in recs:
                        row = _write_data_row(row, bg)
                    pt = _totals(recs)
                    row = _write_subtotal(row, f'Subtotal — {project.name or "(No Project)"}',
                                          pt, f['sub_lbl'], f['sub_num'])
                    for c, v in pt.items():
                        bank_t[c] += v
                row = _write_subtotal(row, f'Bank Total — {bank.name}',
                                       dict(bank_t), f['sub_lbl'], f['sub_num'])
                for c, v in bank_t.items():
                    grand[c] += v
            row = _write_subtotal(row, 'GRAND TOTAL', dict(grand), f['grand_lbl'], f['grand_num'])

        elif self.group_by_bank:
            banks = defaultdict(list)
            for bg in bgs:
                banks[bg.journal_id].append(bg)
            grand = defaultdict(float)
            for bank in sorted(banks, key=lambda b: b.name or ''):
                ws.merge_range(row, 0, row, ncols - 1, f'Bank: {bank.name}', f['group'])
                row += 1
                recs = banks[bank]
                for bg in recs:
                    row = _write_data_row(row, bg)
                bt = _totals(recs)
                row = _write_subtotal(row, f'Bank Total — {bank.name}',
                                       bt, f['sub_lbl'], f['sub_num'])
                for c, v in bt.items():
                    grand[c] += v
            row = _write_subtotal(row, 'GRAND TOTAL', dict(grand), f['grand_lbl'], f['grand_num'])

        elif self.group_by_project:
            projects = defaultdict(list)
            for bg in bgs:
                projects[bg.project_id].append(bg)
            grand = defaultdict(float)
            for project in sorted(projects, key=lambda p: p.name or ''):
                ws.merge_range(row, 0, row, ncols - 1,
                               f'Project: {project.name or "(No Project)"}', f['group'])
                row += 1
                recs = projects[project]
                for bg in recs:
                    row = _write_data_row(row, bg)
                pt = _totals(recs)
                row = _write_subtotal(row, f'Project Total — {project.name or "(No Project)"}',
                                       pt, f['sub_lbl'], f['sub_num'])
                for c, v in pt.items():
                    grand[c] += v
            row = _write_subtotal(row, 'GRAND TOTAL', dict(grand), f['grand_lbl'], f['grand_num'])

        else:
            # Flat export — no grouping
            grand = defaultdict(float)
            for bg in bgs:
                row = _write_data_row(row, bg)
                for i, (_, field, _, ftype) in enumerate(columns):
                    if ftype == 'num':
                        grand[i] += float(_get(bg, field) or 0)
            row = _write_subtotal(row, 'GRAND TOTAL', dict(grand), f['grand_lbl'], f['grand_num'])

        wb.close()
        buf.seek(0)

        attachment = self.env['ir.attachment'].sudo().create({
            'name': 'BG_Registry.xlsx',
            'datas': base64.b64encode(buf.read()),
            'res_model': self._name,
            'res_id': self.id,
            'mimetype': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        })
        return {
            'type': 'ir.actions.act_url',
            'url': f'/web/content/{attachment.id}?download=true',
            'target': 'self',
        }
