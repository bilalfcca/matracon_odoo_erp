"""x.fleet.vehicle.ledger — read-only SQL VIEW combining log entries + spare parts."""

from odoo import models, fields


class FleetVehicleLedger(models.Model):
    _name = 'x.fleet.vehicle.ledger'
    _description = 'Fleet Vehicle Ledger (Log Entries + Spare Parts)'
    _auto = False
    _order = 'date desc, id desc'
    _rec_name = 'vehicle_id'

    # ── Fields matching the SQL VIEW columns ──────────────────────────────────
    vehicle_id = fields.Many2one(
        'fleet.vehicle', string='Vehicle', readonly=True)
    date = fields.Date(string='Date', readonly=True)
    analytic_account_id = fields.Many2one(
        'account.analytic.account', string='Project Site', readonly=True)
    mppl_code = fields.Char(string='MPPL Code', readonly=True)
    license_plate = fields.Char(string='Reg. No.', readonly=True)
    entry_type = fields.Char(string='Type', readonly=True,
                              help='"Log Entry" or "Spare Part"')
    category_label = fields.Char(string='Category', readonly=True)
    sub_category_label = fields.Char(string='Sub-Category', readonly=True)
    description = fields.Text(string='Description / Notes', readonly=True)
    prev_meter = fields.Float(string='Prev. Meter', readonly=True, digits=(16, 2))
    curr_meter = fields.Float(string='Curr. Meter', readonly=True, digits=(16, 2))
    meter_delta = fields.Float(string='Consumption', readonly=True, digits=(16, 2))
    quantity = fields.Float(string='Qty', readonly=True, digits=(16, 3))
    unit_cost = fields.Float(string='Unit Cost (PKR)', readonly=True, digits=(16, 2))
    total_cost = fields.Float(
        string='Cost (PKR)', readonly=True, digits=(16, 2),
        help='Amount from log entry or spare part total cost.')
    driver_name = fields.Char(string='Driver', readonly=True)

    def init(self):
        """Create or replace the SQL VIEW that backs this model."""
        # DROP first to allow column renames / additions
        self.env.cr.execute("DROP VIEW IF EXISTS x_fleet_vehicle_ledger")
        self.env.cr.execute("""
            CREATE OR REPLACE VIEW x_fleet_vehicle_ledger AS

            -- ── Log Entries ───────────────────────────────────────────────
            SELECT
                fvls.id                                         AS id,
                fvls.vehicle_id,
                fvls.date,
                fvls.x_analytic_account_id                      AS analytic_account_id,
                fv.x_mppl_code                                  AS mppl_code,
                fv.license_plate                                AS license_plate,
                'Log Entry'::varchar                            AS entry_type,
                CASE fvls.x_log_category
                    WHEN 'fuel_diesel' THEN 'Fuel'
                    WHEN 'fuel_petrol' THEN 'Fuel'
                    WHEN 'oil_change'  THEN 'Oil Change'
                    WHEN 'air_filter'  THEN 'Filters'
                    WHEN 'oil_filter'  THEN 'Filters'
                    WHEN 'ac_filter'   THEN 'Filters'
                    ELSE 'Other'
                END                                             AS category_label,
                CASE fvls.x_log_category
                    WHEN 'fuel_diesel' THEN 'Fuel — Diesel'
                    WHEN 'fuel_petrol' THEN 'Fuel — Petrol'
                    WHEN 'oil_change'  THEN 'Oil Change'
                    WHEN 'air_filter'  THEN 'Air Filter'
                    WHEN 'oil_filter'  THEN 'Oil Filter'
                    WHEN 'ac_filter'   THEN 'AC Filter'
                    WHEN 'other'       THEN 'Other Consumable'
                    ELSE COALESCE(fvls.x_log_category, 'Other')
                END                                             AS sub_category_label,
                COALESCE(fvls.x_prev_meter, 0.0)               AS prev_meter,
                COALESCE(fvls.x_curr_meter, 0.0)               AS curr_meter,
                COALESCE(fvls.x_meter_delta, 0.0)              AS meter_delta,
                COALESCE(fvls.x_quantity, 1.0)                 AS quantity,
                COALESCE(fvls.x_unit_cost, 0.0)                AS unit_cost,
                COALESCE(fvls.amount, 0.0)                     AS total_cost,
                COALESCE(he.name, '')                           AS driver_name,
                fvls.notes                                      AS description
            FROM fleet_vehicle_log_services fvls
            JOIN fleet_vehicle fv ON fv.id = fvls.vehicle_id
            LEFT JOIN hr_employee he ON he.id = fvls.x_driver_employee_id
            WHERE fvls.x_log_category IS NOT NULL

            UNION ALL

            -- ── Spare Parts ───────────────────────────────────────────────
            SELECT
                (2000000 + xfsp.id)                            AS id,
                xfsp.vehicle_id,
                xfsp.date,
                xfsp.x_analytic_account_id                      AS analytic_account_id,
                xfsp.x_mppl_code                                AS mppl_code,
                fv2.license_plate                               AS license_plate,
                'Spare Part'::varchar                           AS entry_type,
                'Spare Part'::varchar                           AS category_label,
                CASE xfsp.x_spare_category
                    WHEN 'engine'     THEN 'Engine Parts'
                    WHEN 'tyres'      THEN 'Tyres & Wheels'
                    WHEN 'electrical' THEN 'Electrical'
                    WHEN 'filters'    THEN 'Filters'
                    WHEN 'body'       THEN 'Body & Frame'
                    WHEN 'other'      THEN 'Other'
                    ELSE COALESCE(xfsp.x_spare_category, 'Other')
                END                                             AS sub_category_label,
                0.0                                             AS prev_meter,
                COALESCE(xfsp.x_meter_at_service, 0.0)         AS curr_meter,
                0.0                                             AS meter_delta,
                COALESCE(xfsp.quantity, 1.0)                   AS quantity,
                COALESCE(xfsp.x_unit_cost, 0.0)                AS unit_cost,
                COALESCE(xfsp.x_total_cost, 0.0)               AS total_cost,
                ''::varchar                                     AS driver_name,
                xfsp.description
            FROM x_fleet_spare_part xfsp
            JOIN fleet_vehicle fv2 ON fv2.id = xfsp.vehicle_id
            WHERE xfsp.state = 'posted'
        """)
