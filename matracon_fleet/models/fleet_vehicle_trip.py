"""x.fleet.vehicle.trip — Vehicle trip / issuance log (driver + destination + purpose)."""

from odoo import models, fields, api


class FleetVehicleTrip(models.Model):
    _name = 'x.fleet.vehicle.trip'
    _description = 'Fleet Vehicle Trip Record'
    _order = 'date desc, id desc'
    _inherit = ['mail.thread']

    vehicle_id = fields.Many2one(
        'fleet.vehicle', string='Vehicle', required=True,
        ondelete='restrict', index=True, tracking=True)
    x_mppl_code = fields.Char(
        related='vehicle_id.x_mppl_code', store=True, readonly=True, string='MPPL Code')
    x_site_config_id = fields.Many2one(
        'x.project.site.config',
        related='vehicle_id.x_site_config_id',
        store=True, readonly=True, string='Site')
    x_analytic_account_id = fields.Many2one(
        'account.analytic.account',
        related='vehicle_id.x_analytic_account_id',
        store=True, readonly=True, string='Project Site')

    driver_id = fields.Many2one(
        'hr.employee', string='Driver', required=True, tracking=True,
        help='Employee driving this vehicle on this trip.')
    date = fields.Datetime(
        string='Departure Date & Time', required=True,
        default=fields.Datetime.now, tracking=True)
    date_return = fields.Datetime(
        string='Return Date & Time', tracking=True)
    destination = fields.Char(
        string='Destination / Site', required=True, tracking=True)
    purpose = fields.Text(string='Purpose / Remarks')

    start_meter = fields.Float(
        string='Start Meter Reading', digits=(16, 2),
        help='Odometer / hour-meter at departure.')
    end_meter = fields.Float(
        string='End Meter Reading', digits=(16, 2),
        help='Odometer / hour-meter on return.')
    x_meter_unit = fields.Selection(
        related='vehicle_id.x_meter_unit', readonly=True, string='Meter Unit')
    x_distance = fields.Float(
        string='Distance / Usage', compute='_compute_distance', store=True, digits=(16, 2),
        help='end_meter − start_meter. Unit follows vehicle meter type.')

    state = fields.Selection([
        ('pending', 'Pending'),
        ('on_trip', 'On Trip'),
        ('returned', 'Returned'),
    ], string='Status', default='pending', tracking=True)

    @api.depends('start_meter', 'end_meter')
    def _compute_distance(self):
        for rec in self:
            rec.x_distance = max(0.0, (rec.end_meter or 0.0) - (rec.start_meter or 0.0))

    def action_start_trip(self):
        for rec in self:
            rec.state = 'on_trip'

    def action_return(self):
        for rec in self:
            rec.state = 'returned'
