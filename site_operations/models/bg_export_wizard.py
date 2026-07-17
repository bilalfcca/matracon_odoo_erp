"""
Bank Guarantee Registry — Excel Export Wizard

Generates a formatted XLSX file with optional grouping by Bank and/or Project.
Each grouping level gets a section header row and a subtotal row.
Both groupings together produce a Bank → Project two-level hierarchy.
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

# (column_header, bg_field_path, col_width_chars, fmt_type)
# fmt_type: 'text' | 'num' | 'pct' | 'date'
_COLUMNS = [
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

# Column indices for amounts that get summed in subtotal rows
_FIELD_IDX = {field: i for i, (_, field, _, _) in enumerate(_COLUMNS)}
_AMT_COLS = [_FIELD_IDX[f] for f in ('bg_amount', 'margin_amount', 'commission_amount', 'fed_amount')]
_FIRST_AMT = _AMT_COLS[0]   # index 9 — merge label up to here


class BgExportWizard(models.TransientModel):
    _name = 'x.bg.export.wizard'
    _description = 'Bank Guarantee Registry Export'

    group_by_bank = fields.Boolean('Group by Bank', default=True)
    group_by_project = fields.Boolean('Group by Project', default=False)
    include_released = fields.Boolean(
        'Include Released / Expired / Cancelled', default=False,
        help='When unchecked only Draft, Pending, Active, and Locked BGs are exported.')

    def action_export(self):
        self.ensure_one()

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

        # ── Formats ────────────────────────────────────────────────────────
        NAVY = '#003366'
        f = {
            'title':      wb.add_format({'bold': True, 'font_size': 13, 'align': 'center',
                                          'valign': 'vcenter', 'bg_color': NAVY,
                                          'font_color': 'white', 'border': 1}),
            'header':     wb.add_format({'bold': True, 'font_size': 10, 'align': 'center',
                                          'valign': 'vcenter', 'bg_color': NAVY,
                                          'font_color': 'white', 'border': 1, 'text_wrap': True}),
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

        ncols = len(_COLUMNS)
        ws.freeze_panes(2, 0)

        # Column widths
        for c, (_, _, w, _) in enumerate(_COLUMNS):
            ws.set_column(c, c, w)

        # Row 0: title
        ws.merge_range(0, 0, 0, ncols - 1, 'Bank Guarantee Registry', f['title'])
        ws.set_row(0, 22)

        # Row 1: headers
        for c, (label, _, _, _) in enumerate(_COLUMNS):
            ws.write(1, c, label, f['header'])
        ws.set_row(1, 30)

        row = 2

        # ── Field value helpers ─────────────────────────────────────────────
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
            for c, (_, field, _, ftype) in enumerate(_COLUMNS):
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
            return {
                _FIELD_IDX['bg_amount']:          sum(b.bg_amount for b in recs),
                _FIELD_IDX['margin_amount']:       sum(b.margin_amount for b in recs),
                _FIELD_IDX['commission_amount']:   sum(b.commission_amount for b in recs),
                _FIELD_IDX['fed_amount']:          sum(b.fed_amount for b in recs),
            }

        def _write_subtotal(r, label, totals, lbl_fmt, num_fmt):
            """Merge cols 0.._FIRST_AMT-1 for label; write totals in amount cols; blank elsewhere."""
            ws.merge_range(r, 0, r, _FIRST_AMT - 1, label, lbl_fmt)
            for c in range(_FIRST_AMT, ncols):
                if c in _AMT_COLS:
                    ws.write_number(r, c, totals.get(c, 0.0), num_fmt)
                else:
                    ws.write(r, c, '', lbl_fmt)
            return r + 1

        # ── Grouping ────────────────────────────────────────────────────────

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
                for field, col in [('bg_amount', _FIELD_IDX['bg_amount']),
                                    ('margin_amount', _FIELD_IDX['margin_amount']),
                                    ('commission_amount', _FIELD_IDX['commission_amount']),
                                    ('fed_amount', _FIELD_IDX['fed_amount'])]:
                    grand[col] += getattr(bg, field)
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
