"""Extensions to fleet.vehicle — MPPL codes, site assignment, ownership, meter unit."""

from odoo import models, fields, api
from odoo.exceptions import UserError, ValidationError

# Maps x_meter_unit selection values → standard fleet odometer_unit values
# (extended by selection_add below so 'hours' and 'units' are also valid)
_METER_TO_ODOMETER = {
    'km': 'kilometers',
    'hours': 'hours',
    'units': 'units',
}


class FleetVehicleExt(models.Model):
    _inherit = 'fleet.vehicle'

    # ── MPPL Internal Numbering ───────────────────────────────────────────────
    x_mppl_prefix_id = fields.Many2one(
        'x.fleet.mppl.prefix', string='Vehicle / Equipment Type',
        tracking=True,
        help='Select type to auto-generate the MPPL code.')
    x_mppl_code = fields.Char(
        string='MPPL Code', copy=False, tracking=True,
        help='Matracon internal tracking code, e.g. MPPL-G-0001.')

    # ── Fleet Type ────────────────────────────────────────────────────────────
    x_fleet_type = fields.Selection([
        ('ltv', 'LTV — Light Transport Vehicle'),
        ('htv', 'HTV — Heavy Transport Vehicle'),
        ('plant', 'Plant & Equipment'),
    ], string='Fleet Type', tracking=True, required=False)

    # ── Meter Unit ────────────────────────────────────────────────────────────
    x_meter_unit = fields.Selection([
        ('km', 'Kilometers (km)'),
        ('hours', 'Hours (hr)'),
        ('units', 'Units Produced'),
    ], string='Meter Unit', default='km', required=True,
        help='Measurement unit for odometer/hour-meter readings.')

    # Extend the standard odometer_unit to include 'hours' and 'units' so that
    # Odoo's built-in meter log views display the correct unit label.
    odometer_unit = fields.Selection(
        selection_add=[
            ('hours', 'Hours (hr)'),
            ('units', 'Units Produced'),
        ],
        ondelete={'hours': 'set default', 'units': 'set default'},
    )

    # ── Site Assignment ───────────────────────────────────────────────────────
    x_site_config_id = fields.Many2one(
        'x.project.site.config', string='Project Site',
        tracking=True,
        help='Project site this vehicle is deployed at.')
    x_analytic_account_id = fields.Many2one(
        'account.analytic.account', string='Analytic Account',
        related='x_site_config_id.analytic_account_id',
        store=True, readonly=True)

    # ── Ownership ─────────────────────────────────────────────────────────────
    x_ownership_type = fields.Selection([
        ('owned', 'Matracon Owned'),
        ('rented', 'Rented'),
    ], string='Ownership', default='owned', required=True, tracking=True)

    x_rental_vendor_id = fields.Many2one(
        'res.partner', string='Rental Vendor',
        domain=[('supplier_rank', '>', 0)])
    x_rental_rate = fields.Float(string='Rental Rate', digits=(16, 2))
    x_rental_rate_unit = fields.Selection([
        ('daily', 'Per Day'),
        ('monthly', 'Per Month'),
        ('hourly', 'Per Hour'),
    ], string='Rate Unit', default='monthly')
    x_rental_date_start = fields.Date(string='Contract Start')
    x_rental_date_end = fields.Date(string='Contract End')
    x_rental_expiry_alert = fields.Boolean(
        string='Expiry Alert', compute='_compute_rental_expiry_alert', store=True)

    # ── Meter Readings (odometer log — One2many for notebook tab) ────────────
    x_odometer_ids = fields.One2many(
        'fleet.vehicle.odometer', 'vehicle_id', string='Meter Readings')

    # ── Spare Parts (summary) ─────────────────────────────────────────────────
    x_spare_part_ids = fields.One2many(
        'x.fleet.spare.part', 'vehicle_id', string='Spare Parts Log')
    x_spare_part_count = fields.Integer(
        string='Spare Parts', compute='_compute_spare_part_count')

    # ── Computed / Totals ─────────────────────────────────────────────────────
    x_total_log_cost = fields.Monetary(
        string='Total Log Cost', compute='_compute_x_total_costs',
        currency_field='currency_id', store=False)
    x_total_spare_cost = fields.Monetary(
        string='Total Spare Cost', compute='_compute_x_total_costs',
        currency_field='currency_id', store=False)
    x_total_fleet_cost = fields.Monetary(
        string='Total Fleet Cost', compute='_compute_x_total_costs',
        currency_field='currency_id', store=False)

    @api.depends('x_rental_date_end', 'x_ownership_type')
    def _compute_rental_expiry_alert(self):
        from datetime import date, timedelta
        today = date.today()
        for rec in self:
            if rec.x_rental_date_end and rec.x_ownership_type == 'rented':
                rec.x_rental_expiry_alert = rec.x_rental_date_end <= today + timedelta(days=30)
            else:
                rec.x_rental_expiry_alert = False

    def _compute_spare_part_count(self):
        for rec in self:
            rec.x_spare_part_count = len(rec.x_spare_part_ids)

    def _compute_x_total_costs(self):
        for rec in self:
            log_total = sum(rec.log_services.mapped('amount'))
            spare_total = sum(rec.x_spare_part_ids.mapped('x_total_cost'))
            rec.x_total_log_cost = log_total
            rec.x_total_spare_cost = spare_total
            rec.x_total_fleet_cost = log_total + spare_total

    # ── create/write: keep odometer_unit in sync with x_meter_unit ──────────

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if 'x_meter_unit' in vals:
                vals['odometer_unit'] = _METER_TO_ODOMETER.get(
                    vals['x_meter_unit'], 'kilometers')
        return super().create(vals_list)

    def write(self, vals):
        if 'x_meter_unit' in vals:
            vals['odometer_unit'] = _METER_TO_ODOMETER.get(
                vals['x_meter_unit'], 'kilometers')
        return super().write(vals)

    @api.onchange('x_meter_unit')
    def _onchange_x_meter_unit_sync_odometer(self):
        """Keep standard odometer_unit in sync so Odoo's meter log shows the right unit."""
        self.odometer_unit = _METER_TO_ODOMETER.get(self.x_meter_unit, 'kilometers')

    @api.onchange('x_mppl_prefix_id')
    def _onchange_mppl_prefix(self):
        """Auto-generate MPPL code when prefix is selected."""
        if self.x_mppl_prefix_id and not self.x_mppl_code:
            self.x_mppl_code = self.x_mppl_prefix_id.generate_mppl_code()

    @api.onchange('x_ownership_type')
    def _onchange_ownership_type(self):
        if self.x_ownership_type == 'owned':
            self.x_rental_vendor_id = False
            self.x_rental_rate = 0
            self.x_rental_date_start = False
            self.x_rental_date_end = False

    @api.onchange('x_site_config_id')
    def _onchange_site_config(self):
        """Auto-fill location from site name."""
        if self.x_site_config_id:
            self.location = self.x_site_config_id.name

    @api.constrains('x_mppl_code')
    def _check_mppl_unique(self):
        for rec in self:
            if rec.x_mppl_code:
                duplicate = self.search([
                    ('x_mppl_code', '=', rec.x_mppl_code),
                    ('id', '!=', rec.id),
                ])
                if duplicate:
                    raise ValidationError(
                        f"MPPL Code '{rec.x_mppl_code}' is already assigned to another vehicle.")

    def action_view_spare_parts(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': 'Spare Parts Log',
            'res_model': 'x.fleet.spare.part',
            'view_mode': 'list,form',
            'domain': [('vehicle_id', '=', self.id)],
            'context': {'default_vehicle_id': self.id},
        }

    def action_open_vehicle_ledger(self):
        """Open the unified fleet ledger (log entries + spare parts) for this vehicle."""
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': f'Ledger — {self.x_mppl_code or self.name}',
            'res_model': 'x.fleet.vehicle.ledger',
            'view_mode': 'list',
            'domain': [('vehicle_id', '=', self.id)],
            'context': {
                'search_default_group_by_type': 1,
            },
        }
