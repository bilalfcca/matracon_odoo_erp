"""
Bank Guarantee Excel Import Wizard

Reads the Matracon "Bank Wise Outstanding" Excel format:
  - Row 1-3  : title / header (skipped)
  - Row 4    : column headers (skipped)
  - Row 5+   : BG data rows; col B (Sr No.) must be a positive integer

Column mapping (0-based index):
  idx 1  B  Sr No.
  idx 2  C  Bank Name          → journal_id   (lookup by name)
  idx 3  D  LG Reference No.   → guarantee_number
  idx 4  E  Nature of Gtee     → nature_id    (lookup or create)
  idx 5  F  Original Gtee Amt  → bg_amount    (face value; preferred over OS amt)
  idx 6  G  Gtee O/S Amount    → bg_amount    (fallback if F is 0 or unparseable)
  idx 7  H  Entity (MPPL/ZKB)  → ignored (Matracon internal entity type)
  idx 8  I  JV                 → jv_name      (JV partner name)
  idx 9  J  BENIFICIARY        → beneficiary_name (client / employer)
  idx 10 K  PROJECT            → notes        (detailed description)
  idx 11 L  STATUS             → state        (mapped to Odoo selection)
  idx 12 M  AMEND. #           → ignored
  idx 13 N  ISSUANCE DATE      → issue_date
  idx 14 O  EXPIRY DATE        → expiry_date
  idx 15 P  TOTAL LIMIT        → facility.total_limit (update if facility exists)
  idx 18 S  CASH MARGIN %      → cash_margin_percent  (stored as fraction 0.05→5%)
  idx 19 T  CASH MARGIN AMOUNT → ignored (computed)
  idx 22+   Summary table      → ignored entirely
"""
import base64
import datetime as dt
import logging
import re
from io import BytesIO

import openpyxl
from markupsafe import Markup

from odoo import models, fields, api, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

# 0-based column indices matching the Matracon BG Excel template
_C = {
    'sr_no':       1,
    'bank':        2,
    'gno':         3,
    'nature':      4,
    'orig_amt':    5,
    'os_amt':      6,
    'jv_name':     8,
    'beneficiary': 9,
    'description': 10,
    'status':      11,
    'issue_date':  13,
    'expiry_date': 14,
    'total_limit': 15,
    'margin_pct':  18,
}

_STATUS_MAP = {
    'ACTIVE':    'active',
    'RELEASED':  'released',
    'EXPIRED':   'expired',
    'CANCELLED': 'cancelled',
    'PENDING':   'pending',
    'LOCKED':    'locked',
}


# ── Parsing helpers ──────────────────────────────────────────────────────────

def _cell(row, key):
    """Safe column accessor; returns None if column index out of range."""
    idx = _C[key]
    return row[idx] if idx < len(row) else None


def _parse_amount(val):
    """Parse an amount that may be a number OR a string like '697044783- 496,029,401'.
    For strings, returns the last number found (most recent value after amendments)."""
    if val is None or val == 0:
        return 0.0
    if isinstance(val, (int, float)):
        return max(0.0, float(val))
    s = str(val)
    if s.upper() in ('N/A', ''):
        return 0.0
    # Extract all number-like tokens; take the last one (latest amendment value)
    nums = re.findall(r'[\d]+(?:[,\d]*)?(?:\.\d+)?', s.replace(' ', ''))
    for tok in reversed(nums):
        try:
            return float(tok.replace(',', ''))
        except ValueError:
            continue
    return 0.0


def _parse_pct(val):
    """Cash margin % in the Excel is stored as a decimal fraction (0.05 = 5%).
    Multiply by 100 to get the Odoo field value."""
    if val is None or val == '' or str(val).upper() == 'N/A':
        return 0.0
    try:
        return round(float(val) * 100.0, 4)
    except (TypeError, ValueError):
        return 0.0


def _parse_date(val):
    """Return datetime.date or None; rejects Excel date-0 (1900-01-00)."""
    if val is None or val == 0 or val == '' or str(val).upper() == 'N/A':
        return None
    if isinstance(val, dt.datetime):
        d = val.date()
    elif isinstance(val, dt.date):
        d = val
    else:
        return None
    # Reject nonsense dates (before 1990 suggests corrupt/zero Excel date)
    return d if d.year >= 1990 else None


def _is_data_row(row):
    """True only when col B (sr_no) is a positive integer and not a total/blank row."""
    if len(row) <= _C['sr_no']:
        return False
    sr = row[_C['sr_no']]
    if not isinstance(sr, (int, float)) or sr <= 0:
        return False
    bank = str(row[_C['bank']] or '').strip().upper()
    if bank in ('TOTAL', ''):
        return False
    gno = str(row[_C['gno']] or '').strip().upper()
    # Placeholder rows (Bank ISLAMI/UBL/NBP with N/A guarantee numbers)
    if gno == 'N/A':
        return False
    return True


# ── Wizard model ─────────────────────────────────────────────────────────────

class BgImportWizard(models.TransientModel):
    _name = 'x.bg.import.wizard'
    _description = 'Bank Guarantee Excel Import'

    excel_file = fields.Binary('Excel File (.xlsx)', required=True, attachment=False)
    excel_filename = fields.Char('Filename')
    skip_existing = fields.Boolean(
        'Skip BGs whose Guarantee No. already exists', default=True,
        help='Prevents duplicate imports. Uncheck only if you are sure there are no duplicates.')
    update_facility_limits = fields.Boolean(
        'Update Bank Facility Limits from Excel', default=True,
        help='When a bank facility already exists in Odoo, its Total Limit is updated '
             'from the TOTAL LIMIT column (col P) if the Excel value is greater than 0.')
    state = fields.Selection([('draft', 'Ready'), ('done', 'Done')], default='draft')
    result_html = fields.Html('Import Result', readonly=True)
    imported_count = fields.Integer(readonly=True)
    skipped_count = fields.Integer(readonly=True)
    error_count = fields.Integer(readonly=True)

    def action_import(self):
        self.ensure_one()
        if not self.excel_file:
            raise UserError(_('Please upload an Excel file.'))

        try:
            wb = openpyxl.load_workbook(BytesIO(base64.b64decode(self.excel_file)), data_only=True)
        except Exception as e:
            raise UserError(_('Could not open the Excel file: %s') % e)

        ws = wb.active
        all_rows = list(ws.iter_rows(values_only=True))

        # Find the first data row (where col B is a positive integer)
        data_rows = [r for r in all_rows if _is_data_row(r)]
        if not data_rows:
            raise UserError(_(
                'No valid BG rows found. Expected rows where column B contains a Sr No. '
                '(positive integer). Check that you uploaded the correct file.'
            ))

        # Pre-fetch existing guarantee numbers
        existing_gnos = set()
        if self.skip_existing:
            existing_gnos = set(
                self.env['x.bank.guarantee'].sudo().search([]).mapped('guarantee_number')
            )

        # Default nature (Performance Security) as fallback for empty/unknown natures
        default_nature = self.env.ref(
            'site_operations.bg_nature_performance', raise_if_not_found=False)

        # Caches for journal/nature lookups (avoid redundant DB hits)
        journal_cache = {}
        nature_cache = {}

        def _find_journal(name):
            raw = (name or '').strip()
            key = raw.upper()
            if key not in journal_cache:
                # Normalize: collapse spaces around dashes ("MIB- BANK" → "MIB-BANK")
                normalized = re.sub(r'\s*-\s*', '-', raw)
                j = (
                    # 1. Exact case-insensitive match on raw name
                    self.env['account.journal'].sudo().search(
                        [('type', '=', 'bank'), ('name', '=ilike', raw)], limit=1)
                    # 2. Exact on normalized
                    or self.env['account.journal'].sudo().search(
                        [('type', '=', 'bank'), ('name', '=ilike', normalized)], limit=1)
                    # 3. Partial (ilike) on normalized
                    or self.env['account.journal'].sudo().search(
                        [('type', '=', 'bank'), ('name', 'ilike', normalized)], limit=1)
                    # 4. Match on first token before dash (e.g. "MIB" matches "MIB Bank")
                    or self.env['account.journal'].sudo().search(
                        [('type', '=', 'bank'),
                         ('name', 'ilike', normalized.split('-')[0].strip())], limit=1)
                )
                journal_cache[key] = j
            return journal_cache[key]

        def _find_or_create_nature(raw):
            name = (raw or '').strip()
            if not name or name.upper() == 'N/A':
                return default_nature
            key = name.upper()
            if key not in nature_cache:
                n = self.env['x.bg.nature'].sudo().search(
                    [('name', 'ilike', name)], limit=1)
                if not n:
                    n = self.env['x.bg.nature'].sudo().create({'name': name.title()})
                nature_cache[key] = n
            return nature_cache[key]

        imported = []
        skipped = []
        errors = []
        today = fields.Date.context_today(self)

        for row in data_rows:
            sr = int(_cell(row, 'sr_no'))
            bank_name = str(_cell(row, 'bank') or '').strip()
            gno = str(_cell(row, 'gno') or '').strip()

            if not gno:
                skipped.append(f'Sr {sr}: No guarantee number — skipped')
                continue

            if self.skip_existing and gno in existing_gnos:
                skipped.append(f'Sr {sr}: {gno} already exists — skipped')
                continue

            # Bank journal — required; skip row if not found
            journal = _find_journal(bank_name)
            if not journal:
                errors.append(
                    f'Sr {sr} ({gno}): Bank <b>"{bank_name}"</b> not found among bank journals — '
                    f'row skipped. Create the journal in Accounting → Configuration → Journals first.'
                )
                continue

            nature = _find_or_create_nature(_cell(row, 'nature'))

            # BG amount: use O/S (current outstanding) when available; for released BGs
            # where O/S = 0, fall back to the original face value.
            # NOTE: do NOT use orig_amt as primary — it may be a multi-value string
            # like "369417789 -280-270" whose last token is a partial number.
            os_amt = _parse_amount(_cell(row, 'os_amt'))
            orig_amt = _parse_amount(_cell(row, 'orig_amt'))
            bg_amount = os_amt if os_amt > 0 else orig_amt

            issue_date = _parse_date(_cell(row, 'issue_date'))
            expiry_date = _parse_date(_cell(row, 'expiry_date'))

            # Required date fallbacks (flag in notes if using placeholder)
            date_note = ''
            if not issue_date:
                issue_date = today
                date_note += ' [Issue date missing — set to today]'
            if not expiry_date:
                expiry_date = issue_date.replace(year=issue_date.year + 1)
                date_note += ' [Expiry date missing — set to issue + 1 year]'

            # State
            status_raw = str(_cell(row, 'status') or '').strip().upper()
            state = _STATUS_MAP.get(status_raw, 'draft')

            # JV info
            jv_raw = str(_cell(row, 'jv_name') or '').strip()
            is_jv = bool(jv_raw) and jv_raw.upper() not in ('N/A', 'MATRACON PAKISTAN', 'MPPL', 'ZKB')
            jv_type = 'jv' if is_jv else 'direct'
            jv_name_val = jv_raw if is_jv else False

            # Beneficiary = client/employer (col J), description (col K) goes to notes
            beneficiary_name = str(_cell(row, 'beneficiary') or '').strip() or False
            description = str(_cell(row, 'description') or '').strip() or False
            notes_val = description or False
            if date_note:
                notes_val = (notes_val or '') + date_note

            margin_pct = _parse_pct(_cell(row, 'margin_pct'))

            # Auto-link or create facility
            facility = self.env['x.bank.guarantee.facility'].sudo().search(
                [('journal_id', '=', journal.id)], limit=1)

            # Optionally update facility total_limit from col P
            total_limit_val = _parse_amount(_cell(row, 'total_limit'))
            if self.update_facility_limits and facility and total_limit_val > 0:
                if facility.total_limit != total_limit_val:
                    facility.sudo().write({'total_limit': total_limit_val})

            vals = {
                'journal_id':        journal.id,
                'facility_id':       facility.id if facility else False,
                'guarantee_number':  gno,
                'nature_id':         nature.id if nature else default_nature.id if default_nature else False,
                'bg_amount':         bg_amount,
                'jv_type':           jv_type,
                'jv_name':           jv_name_val,
                'beneficiary_name':  beneficiary_name,
                'notes':             notes_val or False,
                'state':             state,
                'issue_date':        issue_date,
                'expiry_date':       expiry_date,
                'cash_margin_percent': margin_pct,
            }

            try:
                bg = self.env['x.bank.guarantee'].sudo().with_context(
                    no_check_limit=True
                ).create(vals)
                imported.append((sr, gno, bank_name, bg.id, bg.name))
                existing_gnos.add(gno)
            except Exception as exc:
                errors.append(f'Sr {sr} ({gno}): <b>ERROR</b> — {exc}')
                _logger.exception('BG import error Sr %s (%s)', sr, gno)

        # ── Build result HTML ────────────────────────────────────────────────
        parts = []
        if imported:
            parts.append(
                f'<div class="alert alert-success mb-2">'
                f'<b>✓ {len(imported)} Bank Guarantee(s) imported successfully.</b>'
                f'</div>'
            )
        if skipped:
            rows_html = ''.join(f'<li>{s}</li>' for s in skipped)
            parts.append(
                f'<div class="alert alert-warning mb-2">'
                f'<b>{len(skipped)} row(s) skipped:</b><ul class="mb-0">{rows_html}</ul>'
                f'</div>'
            )
        if errors:
            rows_html = ''.join(f'<li>{e}</li>' for e in errors)
            parts.append(
                f'<div class="alert alert-danger mb-2">'
                f'<b>{len(errors)} error(s):</b><ul class="mb-0">{rows_html}</ul>'
                f'</div>'
            )
        if imported:
            rows_html = ''.join(
                f'<li>Sr {sr}: <a href="/odoo/bank-guarantees/{bgid}">{bgname}</a> '
                f'— {gno} ({bank})</li>'
                for sr, gno, bank, bgid, bgname in imported
            )
            parts.append(
                f'<details class="mt-2"><summary><b>Imported records ({len(imported)})</b></summary>'
                f'<ul class="mt-1">{rows_html}</ul></details>'
            )

        self.write({
            'state':          'done',
            'result_html':    Markup('\n'.join(parts)),
            'imported_count': len(imported),
            'skipped_count':  len(skipped),
            'error_count':    len(errors),
        })
        # Re-open the same wizard in 'done' state to show results
        return {
            'type':      'ir.actions.act_window',
            'res_model': self._name,
            'res_id':    self.id,
            'view_mode': 'form',
            'target':    'new',
        }

    def action_view_bgs(self):
        return {
            'type':      'ir.actions.act_window',
            'name':      _('Bank Guarantees'),
            'res_model': 'x.bank.guarantee',
            'view_mode': 'list,form',
        }
