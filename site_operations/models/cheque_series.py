from odoo import models, fields, api, _
from odoo.exceptions import UserError, ValidationError


class ChequeSeries(models.Model):
    _name = 'x.cheque.series'
    _description = 'Bank Cheque Series'
    _inherit = ['mail.thread']
    _order = 'bank_journal_id, id'

    name = fields.Char(
        string='Series Name', required=True, tracking=True)
    bank_journal_id = fields.Many2one(
        'account.journal', string='Bank Account',
        required=True, domain=[('type', '=', 'bank')], tracking=True)
    prefix = fields.Char(
        string='Prefix',
        help='Letters before the number, e.g. "HBL-" or "MCB-A-". '
             'Leave blank if the cheque book has numbers only.',
        default='')
    pad_digits = fields.Integer(
        string='Number Width',
        default=6,
        help='Zero-pad the number to this many digits (e.g. 6 → 000001). '
             'Set to 0 for no padding.')

    # ── Number fields stored as Char so no thousand-separator commas are
    #    ever shown in the UI and leading-zeros are preserved (e.g. "000001").
    #    All arithmetic is done via int() conversion in the methods below.
    start_number = fields.Char(
        string='Start Number', required=True, tracking=True,
        help='First cheque number in this series (digits only, e.g. 100001 or 0).')
    end_number = fields.Char(
        string='End Number', required=True, tracking=True,
        help='Last cheque number in this series (inclusive).')
    current_number = fields.Char(
        string='Next Number', tracking=True,
        help='The next cheque number to be issued. Auto-set to Start Number on creation.')

    state = fields.Selection([
        ('active', 'Active'),
        ('exhausted', 'Exhausted'),
        ('closed', 'Closed'),
    ], default='active', tracking=True)

    issued_count = fields.Integer(
        compute='_compute_issued_count', store=False)
    remaining_count = fields.Integer(
        compute='_compute_issued_count', store=False)

    # ── helpers ──────────────────────────────────────────────────────────────

    def _to_int(self, val, default=0):
        """Safely parse a Char number field to int."""
        try:
            return int(val)
        except (TypeError, ValueError):
            return default

    # ── compute ──────────────────────────────────────────────────────────────

    @api.depends('current_number', 'start_number', 'end_number')
    def _compute_issued_count(self):
        for series in self:
            start = series._to_int(series.start_number)
            end = series._to_int(series.end_number)
            current = series._to_int(
                series.current_number or series.start_number, default=start)
            series.issued_count = max(current - start, 0)
            series.remaining_count = max(end - current + 1, 0)

    # ── constraints ──────────────────────────────────────────────────────────

    @api.constrains('start_number', 'end_number', 'current_number')
    def _check_numbers(self):
        for series in self:
            for fname, label in [
                ('start_number', 'Start Number'),
                ('end_number', 'End Number'),
            ]:
                val = getattr(series, fname)
                if val is not None and val != '':
                    try:
                        int(val)
                    except ValueError:
                        raise ValidationError(
                            _('%s must contain digits only (no commas or spaces). '
                              'Got: "%s"') % (label, val))
            if series.current_number not in (None, False, ''):
                try:
                    int(series.current_number)
                except ValueError:
                    raise ValidationError(
                        _('Next Number must contain digits only. Got: "%s"')
                        % series.current_number)
            start = series._to_int(series.start_number)
            end = series._to_int(series.end_number)
            if series.end_number and end < start:
                raise ValidationError(
                    _('End Number (%s) must be greater than or equal to '
                      'Start Number (%s).') % (series.end_number, series.start_number))

    # ── ORM hooks ────────────────────────────────────────────────────────────

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            # Strip any accidental commas/spaces the user might paste in
            for f in ('start_number', 'end_number', 'current_number', 'prefix'):
                if f in vals and vals[f]:
                    vals[f] = str(vals[f]).replace(',', '').strip()
            # Default current_number to start_number (treat '' and None as unset)
            if not vals.get('current_number') and vals.get('current_number') != '0':
                vals['current_number'] = vals.get('start_number') or '1'
        return super().create(vals_list)

    def write(self, vals):
        # Strip any accidental commas/spaces
        for f in ('start_number', 'end_number', 'current_number', 'prefix'):
            if f in vals and vals[f]:
                vals[f] = str(vals[f]).replace(',', '').strip()
        return super().write(vals)

    # ── business logic ───────────────────────────────────────────────────────

    def get_next_cheque_number(self):
        """Return the next formatted cheque number and advance the counter."""
        self.ensure_one()
        if self.state != 'active':
            raise UserError(
                _('Cheque series "%s" is not active.') % self.name)
        current = self._to_int(self.current_number)
        end = self._to_int(self.end_number)
        if current > end:
            self.state = 'exhausted'
            raise UserError(_(
                'Cheque series "%s" is exhausted. '
                'Please create a new series.'
            ) % self.name)
        if self.pad_digits and self.pad_digits > 0:
            cheque_no = '{prefix}{num:0{width}d}'.format(
                prefix=self.prefix or '',
                num=current,
                width=self.pad_digits,
            )
        else:
            cheque_no = '{}{}'.format(self.prefix or '', current)
        next_num = current + 1
        self.current_number = str(next_num)
        if next_num > end:
            self.state = 'exhausted'
        return cheque_no
