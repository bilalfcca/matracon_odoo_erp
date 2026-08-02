"""Fleet configuration singleton + MPPL prefix registry."""

from odoo import models, fields, api
from odoo.exceptions import UserError


class FleetMpplPrefix(models.Model):
    _name = 'x.fleet.mppl.prefix'
    _description = 'MPPL Prefix Code'
    _order = 'prefix_code'

    name = fields.Char(string='Equipment / Vehicle Type', required=True)
    prefix_code = fields.Char(string='Prefix Code', required=True)
    next_sequence = fields.Integer(string='Next Sequence', default=1, required=True)
    active = fields.Boolean(default=True)

    _prefix_code_unique = models.Constraint(
        'UNIQUE(prefix_code)',
        'Prefix code must be unique.',
    )

    def generate_mppl_code(self):
        """Generate the next MPPL code and increment the sequence."""
        self.ensure_one()
        code = f"{self.prefix_code}-{str(self.next_sequence).zfill(4)}"
        self.next_sequence += 1
        return code


class FleetSettings(models.Model):
    _name = 'x.fleet.settings'
    _description = 'Fleet Settings'

    # Singleton enforcement
    @api.model
    def _get_singleton(self):
        rec = self.search([], limit=1)
        if not rec:
            rec = self.create({'name': 'Fleet Settings'})
        return rec

    name = fields.Char(default='Fleet Settings', readonly=True)

    # ── GL Account Mapping ────────────────────────────────────────────────────
    account_fuel_diesel_id = fields.Many2one(
        'account.account', string='Fuel — Diesel',
        domain=[('account_type', 'like', 'expense'), ('deprecated', '=', False)],
        help='GL expense account for diesel fuel log entries.')
    account_fuel_petrol_id = fields.Many2one(
        'account.account', string='Fuel — Petrol',
        domain=[('account_type', 'like', 'expense'), ('deprecated', '=', False)],
        help='GL expense account for petrol fuel log entries.')
    account_oil_id = fields.Many2one(
        'account.account', string='Oil Change',
        domain=[('account_type', 'like', 'expense'), ('deprecated', '=', False)])
    account_filter_id = fields.Many2one(
        'account.account', string='Filters (Air / Oil / AC)',
        domain=[('account_type', 'like', 'expense'), ('deprecated', '=', False)])
    account_spare_parts_id = fields.Many2one(
        'account.account', string='Spare Parts & Maintenance',
        domain=[('account_type', 'like', 'expense'), ('deprecated', '=', False)])
    account_spare_parts_credit_id = fields.Many2one(
        'account.account', string='Spare Parts — Credit Account',
        domain=[('deprecated', '=', False)],
        help='Credit (contra) account for cash spare part purchases (no stock picking). '
             'Typically an accounts payable or petty cash account.')
    account_rental_id = fields.Many2one(
        'account.account', string='Rental Charges',
        domain=[('account_type', 'like', 'expense'), ('deprecated', '=', False)])
    account_other_id = fields.Many2one(
        'account.account', string='Other Consumables',
        domain=[('account_type', 'like', 'expense'), ('deprecated', '=', False)])
    account_log_credit_id = fields.Many2one(
        'account.account', string='Log Entry — Default Credit Account',
        domain=[('deprecated', '=', False)],
        help='Credit (contra) account used when posting a log entry that has NO linked '
             'petty cash expense or vendor bill. Typically a petty cash or cash-in-hand '
             'account. Required only for direct-cash log entries.')

    # ── Operational Settings ──────────────────────────────────────────────────
    fuel_payment_source = fields.Selection([
        ('petty_cash', 'Petty Cash Only'),
        ('vendor_bill', 'Vendor Credit / Bill Only'),
        ('both', 'Both (user selects per entry)'),
    ], string='Fuel Payment Source', default='both', required=True)

    spare_parts_flow = fields.Selection([
        ('inventory', 'Always from Inventory (stock picking required)'),
        ('both', 'Allow Cash Purchase (picking optional)'),
    ], string='Spare Parts Flow', default='inventory', required=True)

    # ── MPPL Prefix Registry ──────────────────────────────────────────────────
    mppl_prefix_ids = fields.One2many(
        'x.fleet.mppl.prefix', compute='_compute_mppl_prefix_ids',
        string='MPPL Prefix Codes', readonly=False)

    def _compute_mppl_prefix_ids(self):
        prefixes = self.env['x.fleet.mppl.prefix'].search([])
        for rec in self:
            rec.mppl_prefix_ids = prefixes

    def get_account_for_category(self, category):
        """Return the GL account for the given log category string."""
        self.ensure_one()
        mapping = {
            'fuel_diesel': self.account_fuel_diesel_id,
            'fuel_petrol': self.account_fuel_petrol_id,
            'oil_change': self.account_oil_id,
            'air_filter': self.account_filter_id,
            'oil_filter': self.account_filter_id,
            'ac_filter': self.account_filter_id,
            'spare_parts': self.account_spare_parts_id,
            'rental': self.account_rental_id,
            'other': self.account_other_id,
        }
        return mapping.get(category, self.account_other_id)

    @api.model
    def action_open_settings(self):
        rec = self._get_singleton()
        return {
            'type': 'ir.actions.act_window',
            'name': 'Fleet Settings',
            'res_model': self._name,
            'res_id': rec.id,
            'view_mode': 'form',
            'target': 'current',
        }
