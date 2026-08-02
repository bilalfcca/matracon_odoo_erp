"""Fleet widget extension for x.site.store.dashboard."""

from datetime import date, timedelta
from odoo import models, fields, api


class SiteStoreDashboardFleet(models.TransientModel):
    _inherit = 'x.site.store.dashboard'

    # ── Fleet KPIs for site ───────────────────────────────────────────────────
    fleet_vehicle_count = fields.Integer(string='Vehicles at Site', readonly=True)
    fleet_fuel_cost_mtd = fields.Monetary(
        string='Fuel Cost (MTD)', readonly=True, currency_field='fleet_currency_id')
    fleet_total_cost_mtd = fields.Monetary(
        string='Total Fleet Cost (MTD)', readonly=True, currency_field='fleet_currency_id')
    fleet_expiring_contracts = fields.Integer(
        string='Expiring Rental Contracts', readonly=True)
    fleet_currency_id = fields.Many2one(
        'res.currency', default=lambda self: self.env.company.currency_id)
    fleet_vehicle_ids = fields.Many2many(
        'fleet.vehicle',
        'site_store_dash_fleet_rel', 'dash_id', 'vehicle_id',
        string='Site Vehicles', readonly=True)

    def _compute_fleet_data(self):
        """Compute fleet KPIs for this site."""
        self.ensure_one()
        today = date.today()
        month_start = today.replace(day=1)

        analytic = self.project_analytic_account_id
        if not analytic:
            self.fleet_vehicle_count = 0
            self.fleet_fuel_cost_mtd = 0
            self.fleet_total_cost_mtd = 0
            self.fleet_expiring_contracts = 0
            self.fleet_vehicle_ids = [(5, 0, 0)]
            return

        vehicles = self.env['fleet.vehicle'].search([
            ('x_analytic_account_id', '=', analytic.id),
            ('active', '=', True),
        ])

        expiring = vehicles.filtered(
            lambda v: v.x_ownership_type == 'rented'
            and v.x_rental_date_end
            and v.x_rental_date_end <= today + timedelta(days=30)
        )

        logs_mtd = self.env['fleet.vehicle.log.services'].search([
            ('vehicle_id', 'in', vehicles.ids),
            ('date', '>=', month_start),
            ('date', '<=', today),
        ])
        fuel_cats = ('fuel_diesel', 'fuel_petrol')
        fuel_mtd = sum(
            logs_mtd.filtered(lambda l: l.x_log_category in fuel_cats).mapped('amount'))
        spare_mtd = sum(
            self.env['x.fleet.spare.part'].search([
                ('vehicle_id', 'in', vehicles.ids),
                ('state', '=', 'posted'),
                ('date', '>=', month_start),
                ('date', '<=', today),
            ]).mapped('x_total_cost')
        )

        self.fleet_vehicle_count = len(vehicles)
        self.fleet_fuel_cost_mtd = fuel_mtd
        self.fleet_total_cost_mtd = fuel_mtd + spare_mtd
        self.fleet_expiring_contracts = len(expiring)
        self.fleet_vehicle_ids = [(6, 0, vehicles.ids)]
