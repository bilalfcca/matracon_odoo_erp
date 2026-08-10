from odoo import models, fields, api, _
from odoo.exceptions import UserError, ValidationError


class ChequeLeaf(models.Model):
    """Individual cheque leaf within a series.

    Each leaf tracks one physical cheque — available → used/discarded.
    Generated in bulk when a ChequeSeries is created.
    """
    _name = 'x.cheque.leaf'
    _description = 'Cheque Leaf'
    _rec_name = 'cheque_number'
    _order = 'series_id, number_int asc'

    series_id = fields.Many2one(
        'x.cheque.series', string='Series',
        required=True, ondelete='cascade', index=True)

    # Stored-related so Many2one domains in payment views can filter by journal
    bank_journal_id = fields.Many2one(
        'account.journal', related='series_id.bank_journal_id',
        store=True, index=True, string='Bank')

    cheque_number = fields.Char(
        string='Cheque No.', required=True, readonly=True)

    # Raw integer for ORDER BY — keeps "000009" < "000010" (not lexicographic)
    number_int = fields.Integer(string='Sort Order', readonly=True)

    state = fields.Selection([
        ('available', 'Available'),
        ('used',      'Used'),
        ('discarded', 'Discarded'),
    ], default='available', string='Status', required=True, index=True)

    # Populated when the leaf is assigned to an actual posted payment
    payment_id = fields.Many2one(
        'account.payment', string='Used in Payment',
        readonly=True, ondelete='set null')

    discarded_reason = fields.Char(string='Discard Reason')
    discarded_date = fields.Date(string='Discarded On', readonly=True)

    # ── actions ──────────────────────────────────────────────────────────────

    def action_discard(self):
        """Mark as Discarded (spoiled / faulty cheque leaf)."""
        for leaf in self:
            if leaf.state == 'used':
                raise UserError(_(
                    'Cannot discard cheque %s — it is already assigned '
                    'to a payment.'
                ) % leaf.cheque_number)
            leaf.write({
                'state': 'discarded',
                'discarded_date': fields.Date.today(),
            })
        return False

    def action_make_available(self):
        """Reset a discarded leaf back to Available (admin correction)."""
        for leaf in self:
            if leaf.state == 'used':
                raise UserError(_(
                    'Cannot reset cheque %s — it has been used in a payment.'
                ) % leaf.cheque_number)
            leaf.write({
                'state': 'available',
                'discarded_date': False,
                'discarded_reason': False,
            })
        return False


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
        help='(Legacy) Next sequential number. '
             'Leaves are now tracked individually in the Cheque Leaves tab.')

    state = fields.Selection([
        ('active', 'Active'),
        ('exhausted', 'Exhausted'),
        ('closed', 'Closed'),
    ], default='active', tracking=True)

    leaf_ids = fields.One2many(
        'x.cheque.leaf', 'series_id', string='Cheque Leaves')

    # ── counts (from leaves, not current_number pointer) ─────────────────────
    issued_count = fields.Integer(
        compute='_compute_leaf_counts', store=False, string='Issued / Used')
    remaining_count = fields.Integer(
        compute='_compute_leaf_counts', store=False, string='Available')
    discarded_count = fields.Integer(
        compute='_compute_leaf_counts', store=False, string='Discarded')

    # ── helpers ──────────────────────────────────────────────────────────────

    def _to_int(self, val, default=0):
        """Safely parse a Char number field to int."""
        try:
            return int(val)
        except (TypeError, ValueError):
            return default

    # ── compute ──────────────────────────────────────────────────────────────

    @api.depends('leaf_ids.state')
    def _compute_leaf_counts(self):
        for series in self:
            leaves = series.leaf_ids
            series.issued_count = len(leaves.filtered(lambda l: l.state == 'used'))
            series.remaining_count = len(leaves.filtered(lambda l: l.state == 'available'))
            series.discarded_count = len(leaves.filtered(lambda l: l.state == 'discarded'))

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
            for f in ('start_number', 'end_number', 'current_number'):
                if f in vals and vals[f]:
                    vals[f] = str(vals[f]).replace(',', '').strip()
            # Default current_number to start_number
            if vals.get('current_number') in (None, False, ''):
                vals['current_number'] = vals.get('start_number') or '1'
        records = super().create(vals_list)
        for series in records:
            series._generate_leaves()
        return records

    def write(self, vals):
        # Strip any accidental commas/spaces
        for f in ('start_number', 'end_number', 'current_number'):
            if f in vals and vals[f]:
                vals[f] = str(vals[f]).replace(',', '').strip()
        return super().write(vals)

    # ── leaf generation ──────────────────────────────────────────────────────

    def _format_cheque_number(self, num):
        """Format a raw integer as the display cheque number for this series."""
        return str(num)

    def _generate_leaves(self):
        """Bulk-create x.cheque.leaf records for this series.

        Numbers that are already below current_number (issued via the old
        sequential auto-assign) are created with state='used' so they don't
        pollute the available dropdown.
        """
        self.ensure_one()
        start = self._to_int(self.start_number)
        end = self._to_int(self.end_number)
        if end < start:
            return
        # Where the sequential pointer sits — anything below is already issued
        current = self._to_int(self.current_number or self.start_number, default=start)

        leaves = []
        for num in range(start, end + 1):
            leaves.append({
                'series_id': self.id,
                'cheque_number': self._format_cheque_number(num),
                'number_int': num,
                'state': 'used' if num < current else 'available',
            })
        if leaves:
            self.env['x.cheque.leaf'].create(leaves)

    def generate_leaves_for_existing(self):
        """Admin action: (re)generate leaves for this series.

        Safe to call on existing series — skips numbers that already have a
        leaf record so it can be re-run without duplicating anything.
        """
        self.ensure_one()
        start = self._to_int(self.start_number)
        end = self._to_int(self.end_number)
        current = self._to_int(self.current_number or self.start_number, default=start)

        existing_nums = set(self.leaf_ids.mapped('number_int'))
        leaves = []
        for num in range(start, end + 1):
            if num in existing_nums:
                continue
            leaves.append({
                'series_id': self.id,
                'cheque_number': self._format_cheque_number(num),
                'number_int': num,
                'state': 'used' if num < current else 'available',
            })
        if leaves:
            self.env['x.cheque.leaf'].create(leaves)
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Leaves Generated'),
                'message': _('%d cheque leaves created for series "%s".') % (
                    len(leaves), self.name),
                'sticky': False,
                'type': 'success',
            },
        }

    # ── business logic ───────────────────────────────────────────────────────

    def get_next_cheque_number(self):
        """Return next available cheque number (leaf-aware).

        Picks the first available leaf.  Falls back to the legacy sequential
        counter when no leaves exist (e.g. old series not yet migrated).
        Also marks the leaf as 'used'.
        """
        self.ensure_one()
        if self.state != 'active':
            raise UserError(
                _('Cheque series "%s" is not active.') % self.name)

        # ── leaf-aware path ──────────────────────────────────────────────────
        leaf = self.env['x.cheque.leaf'].search([
            ('series_id', '=', self.id),
            ('state', '=', 'available'),
        ], order='number_int asc', limit=1)
        if leaf:
            leaf.write({'state': 'used'})
            # Keep sequential pointer in sync (for display / legacy compat)
            next_int = leaf.number_int + 1
            end = self._to_int(self.end_number)
            self.current_number = str(next_int)
            if next_int > end:
                self.state = 'exhausted'
            return leaf.cheque_number

        # ── legacy fallback (no leaves) ──────────────────────────────────────
        current = self._to_int(self.current_number)
        end = self._to_int(self.end_number)
        if current > end:
            self.state = 'exhausted'
            raise UserError(_(
                'Cheque series "%s" is exhausted. '
                'Please create a new series.'
            ) % self.name)
        cheque_no = str(current)
        next_num = current + 1
        self.current_number = str(next_num)
        if next_num > end:
            self.state = 'exhausted'
        return cheque_no
