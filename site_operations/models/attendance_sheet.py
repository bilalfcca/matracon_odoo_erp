import base64
import calendar
import csv
import io
from datetime import date

from markupsafe import Markup

from odoo import models, fields, api, _
from odoo.exceptions import UserError

from . import matracon_notifications as matracon_notify

ATTENDANCE_CODE_SELECTION = [
    ('P', 'P'),
    ('A', 'A'),
    ('L', 'L'),
    ('H', 'H'),
]

DAY_FIELD_NAMES = [f'day_{d:02d}' for d in range(1, 32)]


def _attendance_day_field(day_num):
    return fields.Selection(ATTENDANCE_CODE_SELECTION, string=str(day_num))


class AttendanceLine(models.Model):
    _name = 'x.attendance.line'
    _description = 'Attendance Line'
    _order = 'employee_id'

    sheet_id = fields.Many2one(
        'x.attendance.sheet', required=True, ondelete='cascade', index=True)
    employee_id = fields.Many2one(
        'hr.employee', string='Employee', required=True, index=True)

    # Advance recovery override for this specific month.
    # If set, salary generation uses this value instead of emp.x_advance_balance.
    x_advance_amount = fields.Monetary(
        string='Advance Recovery',
        currency_field='currency_id',
        help='Advance to recover in this month\'s salary. '
             'Leave 0 to use employee\'s current advance balance.',
    )
    currency_id = fields.Many2one(
        'res.currency',
        related='sheet_id.project_analytic_account_id.company_id.currency_id',
        depends=['sheet_id'],
    )

    day_01 = _attendance_day_field(1)
    day_02 = _attendance_day_field(2)
    day_03 = _attendance_day_field(3)
    day_04 = _attendance_day_field(4)
    day_05 = _attendance_day_field(5)
    day_06 = _attendance_day_field(6)
    day_07 = _attendance_day_field(7)
    day_08 = _attendance_day_field(8)
    day_09 = _attendance_day_field(9)
    day_10 = _attendance_day_field(10)
    day_11 = _attendance_day_field(11)
    day_12 = _attendance_day_field(12)
    day_13 = _attendance_day_field(13)
    day_14 = _attendance_day_field(14)
    day_15 = _attendance_day_field(15)
    day_16 = _attendance_day_field(16)
    day_17 = _attendance_day_field(17)
    day_18 = _attendance_day_field(18)
    day_19 = _attendance_day_field(19)
    day_20 = _attendance_day_field(20)
    day_21 = _attendance_day_field(21)
    day_22 = _attendance_day_field(22)
    day_23 = _attendance_day_field(23)
    day_24 = _attendance_day_field(24)
    day_25 = _attendance_day_field(25)
    day_26 = _attendance_day_field(26)
    day_27 = _attendance_day_field(27)
    day_28 = _attendance_day_field(28)
    day_29 = _attendance_day_field(29)
    day_30 = _attendance_day_field(30)
    day_31 = _attendance_day_field(31)

    present_days = fields.Integer(compute='_compute_day_counts', store=True)
    absent_days = fields.Integer(compute='_compute_day_counts', store=True)
    leave_days = fields.Integer(compute='_compute_day_counts', store=True)
    holiday_days = fields.Integer(compute='_compute_day_counts', store=True)
    paid_days = fields.Integer(compute='_compute_day_counts', store=True)
    total_days = fields.Integer(compute='_compute_day_counts', store=True)
    total_display = fields.Char(compute='_compute_day_counts', store=True)

    @api.depends('sheet_id.date_from', 'sheet_id.date_to', *DAY_FIELD_NAMES)
    def _compute_day_counts(self):
        for line in self:
            days_in_month = 0
            if line.sheet_id.date_from:
                days_in_month = calendar.monthrange(
                    line.sheet_id.date_from.year,
                    line.sheet_id.date_from.month,
                )[1]
            present = absent = leave = holiday = 0
            for d in range(1, days_in_month + 1):
                code = getattr(line, f'day_{d:02d}', False)
                if code == 'P':
                    present += 1
                elif code == 'A':
                    absent += 1
                elif code == 'L':
                    leave += 1
                elif code == 'H':
                    holiday += 1
            line.present_days = present
            line.absent_days = absent
            line.leave_days = leave
            line.holiday_days = holiday
            line.paid_days = present + holiday
            line.total_days = days_in_month
            line.total_display = f'{line.paid_days}/{days_in_month}'


class AttendanceSheet(models.Model):
    _name = 'x.attendance.sheet'
    _description = 'Monthly Attendance Registry'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'date_from desc, id desc'

    name = fields.Char(compute='_compute_name', store=True, readonly=True)
    project_analytic_account_id = fields.Many2one(
        'account.analytic.account', string='Site Project',
        required=True, tracking=True, index=True,
    )
    date_from = fields.Date(required=True, tracking=True)
    date_to = fields.Date(required=True, tracking=True)
    state = fields.Selection([
        ('draft', 'Draft'),
        ('verified', 'Verified'),
        ('posted', 'Posted'),
    ], default='draft', tracking=True, required=True)

    uploaded_file = fields.Binary(string='Uploaded Attendance Sheet')
    uploaded_filename = fields.Char()

    line_ids = fields.One2many(
        'x.attendance.line', 'sheet_id', string='Employees')

    employee_count = fields.Integer(
        compute='_compute_stats', string='Workforce')
    avg_present_pct = fields.Float(
        compute='_compute_stats', string='Monthly Present %')
    total_present_days = fields.Integer(
        compute='_compute_stats', string='Present Days')
    total_absent_days = fields.Integer(
        compute='_compute_stats', string='Absent Days')

    salary_sheet_id = fields.Many2one(
        'x.salary.sheet', string='Salary Sheet', readonly=True, copy=False)

    @api.depends('project_analytic_account_id', 'date_from', 'date_to')
    def _compute_name(self):
        for sheet in self:
            proj = (
                sheet.project_analytic_account_id.code
                or sheet.project_analytic_account_id.name or ''
            )
            month = sheet.date_from.strftime('%b %Y') if sheet.date_from else ''
            sheet.name = f'Attendance/{proj}/{month}' if proj else f'Attendance/{month}'

    @api.depends('line_ids', 'line_ids.present_days', 'line_ids.absent_days',
                 'line_ids.total_days', 'date_from', 'date_to')
    def _compute_stats(self):
        for sheet in self:
            lines = sheet.line_ids
            sheet.employee_count = len(lines)
            sheet.total_present_days = sum(lines.mapped('present_days'))
            sheet.total_absent_days = sum(lines.mapped('absent_days'))
            total_slots = sum(lines.mapped('total_days'))
            sheet.avg_present_pct = (
                (sheet.total_present_days / total_slots * 100.0)
                if total_slots else 0.0
            )

    @api.onchange('date_from')
    def _onchange_date_from(self):
        if self.date_from:
            last_day = calendar.monthrange(
                self.date_from.year, self.date_from.month)[1]
            self.date_to = date(
                self.date_from.year, self.date_from.month, last_day)

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        user = self.env.user
        if not res.get('project_analytic_account_id'):
            analytic = getattr(user, 'x_default_analytic_account_id', False)
            if analytic:
                res['project_analytic_account_id'] = analytic.id
        if res.get('date_from') and not res.get('date_to'):
            d = res['date_from']
            if isinstance(d, str):
                d = fields.Date.from_string(d)
            last = calendar.monthrange(d.year, d.month)[1]
            res['date_to'] = date(d.year, d.month, last)
        return res

    # ── CSV / Excel Import ────────────────────────────────────────────────────

    def _parse_excel_rows(self, file_content):
        """Return list-of-lists from an xlsx file using openpyxl."""
        try:
            import openpyxl
        except ImportError:
            raise UserError(_(
                'openpyxl is not installed. Please upload a CSV file instead.'))
        wb = openpyxl.load_workbook(
            io.BytesIO(file_content), read_only=True, data_only=True)
        ws = wb.active
        rows = []
        for row in ws.iter_rows(values_only=True):
            rows.append([str(c).strip() if c is not None else '' for c in row])
        wb.close()
        return rows

    def _parse_csv_rows(self, file_content):
        """Return list-of-lists from a CSV/text file."""
        text = file_content.decode('utf-8-sig', errors='replace')
        reader = csv.reader(io.StringIO(text))
        return [[cell.strip() for cell in row] for row in reader]

    def action_import_attendance(self):
        """
        Parse the uploaded CSV/Excel file and populate attendance lines.

        Expected format (header row):
            Employee Name | Month | 1 | 2 | 3 | … | 31 | Advance
        Codes: P = Present, A = Absent, L = Paid Leave, H = Public Holiday

        The optional 'Month' column in the header is used for validation only
        (not per-row data). The first data row's Month cell is read and
        compared against this sheet's date_from month/year. If they differ,
        the import is blocked with a clear error message.
        """
        VALID_CODES = {'P', 'A', 'L', 'H'}
        MONTH_NAMES = {
            'jan': 1, 'feb': 2, 'mar': 3, 'apr': 4, 'may': 5, 'jun': 6,
            'jul': 7, 'aug': 8, 'sep': 9, 'oct': 10, 'nov': 11, 'dec': 12,
            'january': 1, 'february': 2, 'march': 3, 'april': 4,
            'june': 6, 'july': 7, 'august': 8, 'september': 9,
            'october': 10, 'november': 11, 'december': 12,
        }

        for sheet in self:
            if not sheet.uploaded_file:
                raise UserError(_('Upload an attendance file before importing.'))
            if sheet.state != 'draft':
                raise UserError(_('Only draft sheets can be imported.'))
            if not sheet.date_from:
                raise UserError(_(
                    'Please set the Date From (month) on this sheet before importing.'
                ))

            raw = base64.b64decode(sheet.uploaded_file)
            fname = (sheet.uploaded_filename or '').lower()
            if fname.endswith(('.xlsx', '.xls')):
                rows = sheet._parse_excel_rows(raw)
            else:
                rows = sheet._parse_csv_rows(raw)

            # Filter completely empty rows
            rows = [r for r in rows if any(c for c in r)]
            if not rows:
                raise UserError(_('The uploaded file appears to be empty.'))

            # ── Parse header → map day columns + optional Month/Advance columns ──
            header = rows[0]
            day_col = {}      # day_num (int) → column index
            advance_col = None
            month_col = None  # optional month validation column

            for idx, cell in enumerate(header):
                cell_lower = cell.lower().strip()
                if cell_lower in ('advance', 'advance recovery', 'advance amount'):
                    advance_col = idx
                    continue
                if cell_lower in ('month', 'month/year', 'month year', 'period'):
                    month_col = idx
                    continue
                try:
                    # Excel returns numeric headers as floats (1.0, 2.0 …)
                    # so use int(float(cell)) to handle both '1' and '1.0'
                    day_num = int(float(cell))
                    if 1 <= day_num <= 31:
                        day_col[day_num] = idx
                except (ValueError, TypeError):
                    pass

            # ── Month/Year validation: read from first data row if column present ──
            if month_col is not None and len(rows) > 1 and sheet.date_from:
                first_data_row = rows[1]
                if month_col < len(first_data_row):
                    raw_month = first_data_row[month_col].strip()
                    csv_month, csv_year = None, None
                    # Try formats: "May 2026", "2026-05", "05/2026", "05-2026", "May-2026"
                    import re
                    # Format: Month_Name YYYY  e.g. "May 2026" or "May-2026"
                    m = re.match(
                        r'([A-Za-z]+)[\s\-/]+(\d{4})', raw_month)
                    if m:
                        csv_month = MONTH_NAMES.get(m.group(1).lower())
                        csv_year = int(m.group(2))
                    else:
                        # Format: YYYY-MM  e.g. "2026-05"
                        m = re.match(r'(\d{4})[-/](\d{1,2})', raw_month)
                        if m:
                            csv_year = int(m.group(1))
                            csv_month = int(m.group(2))
                        else:
                            # Format: MM/YYYY or MM-YYYY  e.g. "05/2026"
                            m = re.match(r'(\d{1,2})[-/](\d{4})', raw_month)
                            if m:
                                csv_month = int(m.group(1))
                                csv_year = int(m.group(2))

                    if csv_month and csv_year:
                        sheet_month = sheet.date_from.month
                        sheet_year = sheet.date_from.year
                        if csv_month != sheet_month or csv_year != sheet_year:
                            csv_label = f'{raw_month}'
                            sheet_label = sheet.date_from.strftime('%B %Y')
                            raise UserError(_(
                                'Month mismatch!\n\n'
                                'CSV file is for: %s\n'
                                'This attendance sheet is for: %s\n\n'
                                'Please upload the correct file for %s, '
                                'or create a new attendance sheet for %s.'
                            ) % (csv_label, sheet_label, sheet_label, csv_label))

            if not day_col:
                raise UserError(_(
                    'Could not find day columns in the header row.\n'
                    'Expected format: Employee Name | Month | 1 | 2 | … | 30 | Advance\n\n'
                    'Day columns should be the numbers 1 to 28/29/30/31 depending on the month. '
                    'June = 1 to 30, July = 1 to 31, etc.'))

            Employee = self.env['hr.employee'].sudo()
            imported = 0
            skipped = []

            for row_num, row in enumerate(rows[1:], start=2):
                if not row:
                    continue
                emp_name = row[0].strip() if row else ''
                if not emp_name:
                    continue

                # Find employee — prefer same project, fall back to any match
                emp = Employee.search([
                    ('name', 'ilike', emp_name),
                    ('x_project_analytic_account_id', '=',
                     sheet.project_analytic_account_id.id),
                ], limit=1)
                if not emp:
                    emp = Employee.search(
                        [('name', 'ilike', emp_name)], limit=1)
                if not emp:
                    skipped.append(
                        _('Row %d: "%s" — employee not found') % (row_num, emp_name))
                    continue

                # Find or create attendance line
                att_line = sheet.line_ids.filtered(
                    lambda l: l.employee_id.id == emp.id)
                if not att_line:
                    att_line = self.env['x.attendance.line'].create({
                        'sheet_id': sheet.id,
                        'employee_id': emp.id,
                    })
                else:
                    att_line = att_line[0]

                # Write day codes
                vals = {}
                for day_num, col_idx in day_col.items():
                    if col_idx < len(row):
                        code = row[col_idx].upper()
                        if code in VALID_CODES:
                            vals[f'day_{day_num:02d}'] = code

                # Write advance recovery override if column present
                if advance_col is not None and advance_col < len(row):
                    try:
                        adv = float(str(row[advance_col]).replace(',', '') or 0)
                        if adv >= 0:
                            vals['x_advance_amount'] = adv
                    except (ValueError, TypeError):
                        pass

                if vals:
                    att_line.write(vals)
                imported += 1

            # Post result to chatter
            summary = Markup(
                '<b>Attendance Import Result</b><br/>'
                'Imported: <b>{imported}</b> employee(s).'
            ).format(imported=imported)
            if skipped:
                summary += Markup('<br/><b>Skipped:</b><br/>') + Markup(
                    '<br/>').join(Markup(s) for s in skipped[:20])
            sheet.message_post(body=summary)

            if skipped:
                raise UserError(
                    _('Import completed with %d warning(s):\n\n%s')
                    % (len(skipped), '\n'.join(skipped[:20])))

    def action_load_employees(self):
        """Populate lines from project employees."""
        for sheet in self:
            if sheet.state != 'draft':
                raise UserError(_('Only draft sheets can be refreshed.'))
            employees = self.env['hr.employee'].sudo().search([
                ('x_project_analytic_account_id', '=',
                 sheet.project_analytic_account_id.id),
            ])
            existing = {l.employee_id.id: l for l in sheet.line_ids}
            for emp in employees:
                if emp.id not in existing:
                    self.env['x.attendance.line'].create({
                        'sheet_id': sheet.id,
                        'employee_id': emp.id,
                    })
            sheet.message_post(body=_('Employee list refreshed from project roster.'))

    def action_verify(self):
        for sheet in self:
            if not sheet.line_ids:
                raise UserError(_('Add employees before verifying attendance.'))
            # Check that at least one day code has been recorded
            has_data = any(
                getattr(line, f'day_{d:02d}', False)
                for line in sheet.line_ids
                for d in range(1, 32)
            )
            if not has_data:
                raise UserError(_(
                    'No attendance data found. '
                    'Please upload your file and click "Import from File" '
                    'before verifying.'
                ))
            sheet.state = 'verified'
            sheet.message_post(
                body=Markup(_('Attendance verified by <b>%s</b>.')) % self.env.user.name)

    def action_post(self):
        for sheet in self:
            if sheet.state != 'verified':
                raise UserError(_('Verify attendance before posting.'))
            sheet.state = 'posted'
            salary = sheet._generate_salary_sheet()
            sheet.message_post(
                body=Markup(_(
                    'Attendance posted. Salary sheet <b>%s</b> generated.'
                )) % salary.name)
            fo_users = self.env['res.users'].search([
                ('group_ids', 'in', self.env.ref(
                    'site_operations.group_finance_ho').id),
            ])
            matracon_notify.notify_users(
                sheet,
                fo_users,
                _('Attendance posted for <b>%s</b> — salary sheet ready.') % sheet.name,
                summary=_('Attendance Posted'),
            )

    def _generate_salary_sheet(self):
        self.ensure_one()
        existing = self.env['x.salary.sheet'].search([
            ('attendance_sheet_id', '=', self.id),
        ], limit=1)
        if existing:
            return existing
        return self.env['x.salary.sheet'].create({
            'attendance_sheet_id': self.id,
            'project_analytic_account_id': self.project_analytic_account_id.id,
            'date_from': self.date_from,
            'date_to': self.date_to,
        })

    def action_print_attendance_sheet(self):
        return self.env.ref(
            'site_operations.action_report_attendance_sheet'
        ).report_action(self)
