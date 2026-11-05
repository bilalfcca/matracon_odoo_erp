"""Fleet smart buttons on the Project form (see site_operations project_project_drilldown)."""

from odoo import _, fields, models


class ProjectProjectFleet(models.Model):
    _inherit = 'project.project'

    x_mtr_vehicle_count = fields.Integer(compute='_compute_mtr_counts', string='Vehicles')
    x_mtr_trip_count = fields.Integer(compute='_compute_mtr_counts', string='Vehicle Trips')
    x_mtr_vehicle_ledger_count = fields.Integer(compute='_compute_mtr_counts', string='Vehicle Ledger')

    def _mtr_drilldowns(self):
        specs = super()._mtr_drilldowns()
        aid = self.x_analytic_account_id.id
        if not aid:
            return specs
        specs.update({
            'vehicle': {
                'model': 'fleet.vehicle', 'action': 'fleet.fleet_vehicle_action',
                'name': _('Vehicles'), 'count_field': 'x_mtr_vehicle_count',
                'domain': [('x_analytic_account_id', '=', aid)],
            },
            'trip': {
                'model': 'x.fleet.vehicle.trip', 'action': 'matracon_fleet.action_fleet_vehicle_trip_all',
                'name': _('Vehicle Trips'), 'count_field': 'x_mtr_trip_count',
                'domain': [('x_analytic_account_id', '=', aid)],
            },
            'vehicle_ledger': {
                'model': 'x.fleet.vehicle.ledger', 'action': 'matracon_fleet.action_fleet_vehicle_ledger',
                'name': _('Vehicle Ledger'), 'count_field': 'x_mtr_vehicle_ledger_count',
                'domain': [('analytic_account_id', '=', aid)],
            },
        })
        return specs
