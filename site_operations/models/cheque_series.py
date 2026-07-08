from odoo import models, fields, api, _
from odoo.exceptions import UserError


class ChequeSeries(models.Model):
    _name = 'x.cheque.series'
    _description = 'Bank Cheque Series'
    _inherit = ['mail.thread']
    _order = 'bank_journal_id, start_number'

    name = fields.Char(
        string='Series Name', required=True, tracking=True)
    bank_journal_id = fields.Many2one(
        'account.journal', string='Bank Account',
        required=True, domain=[('type', '=', 'bank')], tracking=True)
    prefix = fields.Char(
        string='Prefix',
        help='Letters/digits before the number, e.g. HBL- or MCB-A-', default='')
    pad_digits = fields.Integer(
        string='Number Width',
        default=6,
        help='Zero-pad the number to this many digits (e.g. 6 → 000001). '
             'Set to 0 for no padding (useful when number already contains letters).')
    start_number = fields.Integer(
        string='Start Number', required=True, tracking=True)
    end_number = fields.Integer(
        string='End Number', required=True, tracking=True)
    current_number = fields.Integer(
        string='Next Number', tracking=True)
    state = fields.Selection([
        ('active', 'Active'),
        ('exhausted', 'Exhausted'),
        ('closed', 'Closed'),
    ], default='active', tracking=True)
    issued_count = fields.Integer(
        compute='_compute_issued_count', store=False)
    remaining_count = fields.Integer(
        compute='_compute_issued_count', store=False)

    @api.depends('current_number', 'start_number', 'end_number')
    def _compute_issued_count(self):
        for series in self:
            current = series.current_number or series.start_number
            series.issued_count = max(current - series.start_number, 0)
            series.remaining_count = max(
                series.end_number - current + 1, 0)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if not vals.get('current_number'):
                vals['current_number'] = vals.get('start_number', 1)
        return super().create(vals_list)

    def get_next_cheque_number(self):
        """Returns the next formatted cheque number and advances the counter."""
        self.ensure_one()
        if self.state != 'active':
            raise UserError(
                _('Cheque series "%s" is not active.') % self.name)
        if self.current_number > self.end_number:
            self.state = 'exhausted'
            raise UserError(_(
                'Cheque series "%s" is exhausted. '
                'Please create a new series.'
            ) % self.name)
        if self.pad_digits and self.pad_digits > 0:
            cheque_no = f'{self.prefix}{self.current_number:0{self.pad_digits}d}'
        else:
            cheque_no = f'{self.prefix}{self.current_number}'
        self.current_number += 1
        if self.current_number > self.end_number:
            self.state = 'exhausted'
        return cheque_no
