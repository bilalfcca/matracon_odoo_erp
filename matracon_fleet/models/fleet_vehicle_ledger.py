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
    entry_type = fields.Char(string='Type', readonly=True,
                              help='"Log Entry" or "Spare Part"')
    category = fields.Char(string='Category', readonly=True,
                            help='Log category or spare part category code')
    description = fields.Text(string='Description / Notes', readonly=True)
    total_cost = fields.Float(
        string='Cost (PKR)', readonly=True, digits=(16, 2),
        help='Amount from log entry or spare part total cost.')

    def init(self):
        """Create or replace the SQL VIEW that backs this model."""
        self.env.cr.execute("""
            CREATE OR REPLACE VIEW x_fleet_vehicle_ledger AS
            SELECT
                fvls.id                                     AS id,
                fvls.vehicle_id,
                fvls.date,
                fvls.x_analytic_account_id                  AS analytic_account_id,
                fv.x_mppl_code                              AS mppl_code,
                'Log Entry'::varchar                        AS entry_type,
                fvls.x_log_category                        AS category,
                fvls.notes                                  AS description,
                COALESCE(fvls.amount, 0.0)                 AS total_cost
            FROM fleet_vehicle_log_services fvls
            JOIN fleet_vehicle fv ON fv.id = fvls.vehicle_id
            WHERE fvls.x_log_category IS NOT NULL

            UNION ALL

            SELECT
                (2000000 + xfsp.id)                        AS id,
                xfsp.vehicle_id,
                xfsp.date,
                xfsp.x_analytic_account_id                  AS analytic_account_id,
                xfsp.x_mppl_code                            AS mppl_code,
                'Spare Part'::varchar                       AS entry_type,
                xfsp.x_spare_category                      AS category,
                xfsp.description,
                COALESCE(xfsp.x_total_cost, 0.0)           AS total_cost
            FROM x_fleet_spare_part xfsp
            WHERE xfsp.state = 'posted'
        """)
