"""Fleet KPI extensions to x.management.dashboard."""

import logging
from odoo import models, fields, api

_logger = logging.getLogger(__name__)


# ── Fleet line models ────────────────────────────────────────────────────────

class ManagementDashboardFleetSiteLine(models.TransientModel):
    _name = 'x.management.dashboard.fleet.site.line'
    _description = 'Fleet Dashboard — Cost by Site'
    _order = 'total_cost desc'

    dashboard_id = fields.Many2one(
        'x.management.dashboard', ondelete='cascade', required=True)
    analytic_account_id = fields.Many2one('account.analytic.account', readonly=True)
    site_name = fields.Char(readonly=True)
    vehicle_count = fields.Integer(readonly=True)
    fuel_cost = fields.Monetary(readonly=True, currency_field='currency_id')
    spare_cost = fields.Monetary(readonly=True, currency_field='currency_id')
    rental_cost = fields.Monetary(readonly=True, currency_field='currency_id')
    other_cost = fields.Monetary(readonly=True, currency_field='currency_id')
    total_cost = fields.Monetary(readonly=True, currency_field='currency_id')
    currency_id = fields.Many2one('res.currency', readonly=True)


class ManagementDashboardFleetVehicleLine(models.TransientModel):
    _name = 'x.management.dashboard.fleet.vehicle.line'
    _description = 'Fleet Dashboard — Cost by Vehicle'
    _order = 'total_cost desc'

    dashboard_id = fields.Many2one(
        'x.management.dashboard', ondelete='cascade', required=True)
    vehicle_id = fields.Many2one('fleet.vehicle', readonly=True)
    vehicle_name = fields.Char(readonly=True)
    mppl_code = fields.Char(readonly=True)
    fleet_type = fields.Char(readonly=True)
    site_name = fields.Char(readonly=True)
    log_cost = fields.Monetary(readonly=True, currency_field='currency_id')
    spare_cost = fields.Monetary(readonly=True, currency_field='currency_id')
    total_cost = fields.Monetary(readonly=True, currency_field='currency_id')
    currency_id = fields.Many2one('res.currency', readonly=True)


# ── Management Dashboard extension ──────────────────────────────────────────

_FLEET_LINE_FIELDS = frozenset({
    'fleet_site_line_ids',
    'fleet_vehicle_line_ids',
})


class ManagementDashboardFleet(models.TransientModel):
    _inherit = 'x.management.dashboard'

    # ── Fleet KPIs ────────────────────────────────────────────────────────────
    kpi_fleet_total = fields.Integer(string='Total Vehicles', readonly=True)
    kpi_fleet_ltv = fields.Integer(string='LTV Count', readonly=True)
    kpi_fleet_htv = fields.Integer(string='HTV Count', readonly=True)
    kpi_fleet_plant = fields.Integer(string='Plant & Equipment', readonly=True)
    kpi_fleet_owned = fields.Integer(string='Owned', readonly=True)
    kpi_fleet_rented = fields.Integer(string='Rented', readonly=True)

    kpi_fleet_total_cost = fields.Monetary(
        string='Total Fleet Cost', readonly=True, currency_field='currency_id')
    kpi_fleet_fuel_cost = fields.Monetary(
        string='Fuel Cost', readonly=True, currency_field='currency_id')
    kpi_fleet_spare_cost = fields.Monetary(
        string='Spare Parts Cost', readonly=True, currency_field='currency_id')
    kpi_fleet_rental_cost = fields.Monetary(
        string='Rental Cost', readonly=True, currency_field='currency_id')
    kpi_fleet_other_cost = fields.Monetary(
        string='Other Cost', readonly=True, currency_field='currency_id')

    fleet_site_line_ids = fields.One2many(
        'x.management.dashboard.fleet.site.line', 'dashboard_id',
        string='Fleet Cost by Site', readonly=True)
    fleet_vehicle_line_ids = fields.One2many(
        'x.management.dashboard.fleet.vehicle.line', 'dashboard_id',
        string='Top Vehicles by Cost', readonly=True)

    # ── Fleet KPI computation ─────────────────────────────────────────────────

    def _compute_fleet_kpis(self):
        """Compute all fleet KPIs respecting scope and date filters."""
        self.ensure_one()
        date_from, date_to = self._resolve_date_range()
        analytics = self._active_analytic_accounts()
        analytic_ids = analytics.ids
        currency = self.env.company.currency_id

        Vehicle = self.env['fleet.vehicle']
        LogEntry = self.env['fleet.vehicle.log.services']
        SparePart = self.env['x.fleet.spare.part']

        # Vehicle domain — filter by site analytics if scoped
        v_domain = [('active', '=', True)]
        if analytic_ids:
            v_domain.append(('x_analytic_account_id', 'in', analytic_ids))

        vehicles = Vehicle.search(v_domain)

        kpi_total = len(vehicles)
        kpi_ltv = len(vehicles.filtered(lambda v: v.x_fleet_type == 'ltv'))
        kpi_htv = len(vehicles.filtered(lambda v: v.x_fleet_type == 'htv'))
        kpi_plant = len(vehicles.filtered(lambda v: v.x_fleet_type == 'plant'))
        kpi_owned = len(vehicles.filtered(lambda v: v.x_ownership_type == 'owned'))
        kpi_rented = len(vehicles.filtered(lambda v: v.x_ownership_type == 'rented'))

        # Log entry costs (period-filtered)
        log_domain = [('vehicle_id', 'in', vehicles.ids)]
        if date_from:
            log_domain.append(('date', '>=', date_from))
        if date_to:
            log_domain.append(('date', '<=', date_to))

        log_entries = LogEntry.search(log_domain)
        fuel_cats = ('fuel_diesel', 'fuel_petrol')
        other_cats = ('oil_change', 'air_filter', 'oil_filter', 'ac_filter', 'other')

        fuel_cost = sum(
            log_entries.filtered(lambda l: l.x_log_category in fuel_cats).mapped('amount'))
        other_cost = sum(
            log_entries.filtered(lambda l: l.x_log_category in other_cats).mapped('amount'))

        # Spare parts costs (period-filtered, posted only)
        spare_domain = [('vehicle_id', 'in', vehicles.ids), ('state', '=', 'posted')]
        if date_from:
            spare_domain.append(('date', '>=', date_from))
        if date_to:
            spare_domain.append(('date', '<=', date_to))
        spare_cost = sum(SparePart.search(spare_domain).mapped('x_total_cost'))

        # Per-site breakdown
        site_lines = []
        sites = self.env['project.site.config'].search(
            [('x_analytic_account_id', 'in', analytic_ids)] if analytic_ids else [])
        for site in sites:
            site_vehicles = vehicles.filtered(
                lambda v, s=site: v.x_site_config_id.id == s.id)
            if not site_vehicles:
                continue
            site_vehicle_ids = site_vehicles.ids
            site_logs = log_entries.filtered(
                lambda l, ids=site_vehicle_ids: l.vehicle_id.id in ids)
            site_spares = SparePart.search(
                spare_domain + [('vehicle_id', 'in', site_vehicle_ids)])
            s_fuel = sum(
                site_logs.filtered(lambda l: l.x_log_category in fuel_cats).mapped('amount'))
            s_other = sum(
                site_logs.filtered(lambda l: l.x_log_category in other_cats).mapped('amount'))
            s_spare = sum(site_spares.mapped('x_total_cost'))
            s_total = s_fuel + s_other + s_spare
            site_lines.append({
                'analytic_account_id': site.x_analytic_account_id.id,
                'site_name': site.name,
                'vehicle_count': len(site_vehicles),
                'fuel_cost': s_fuel,
                'spare_cost': s_spare,
                'rental_cost': 0.0,
                'other_cost': s_other,
                'total_cost': s_total,
                'currency_id': currency.id,
            })

        # Per-vehicle top 20 by cost
        vehicle_lines = []
        fleet_type_labels = dict(
            self.env['fleet.vehicle']._fields['x_fleet_type'].selection or [])
        for v in vehicles:
            v_ids = [v.id]
            v_logs = log_entries.filtered(lambda l, ids=v_ids: l.vehicle_id.id in ids)
            v_spares = SparePart.search(spare_domain + [('vehicle_id', '=', v.id)])
            v_log_cost = sum(v_logs.mapped('amount'))
            v_spare_cost = sum(v_spares.mapped('x_total_cost'))
            v_total = v_log_cost + v_spare_cost
            if v_total == 0:
                continue
            vehicle_lines.append({
                'vehicle_id': v.id,
                'vehicle_name': v.name,
                'mppl_code': v.x_mppl_code or '',
                'fleet_type': fleet_type_labels.get(v.x_fleet_type, '') if v.x_fleet_type else '',
                'site_name': v.x_site_config_id.name if v.x_site_config_id else '',
                'log_cost': v_log_cost,
                'spare_cost': v_spare_cost,
                'total_cost': v_total,
                'currency_id': currency.id,
            })
        vehicle_lines.sort(key=lambda x: x['total_cost'], reverse=True)
        vehicle_lines = vehicle_lines[:20]

        total_cost = fuel_cost + other_cost + spare_cost

        return {
            'kpi_fleet_total': kpi_total,
            'kpi_fleet_ltv': kpi_ltv,
            'kpi_fleet_htv': kpi_htv,
            'kpi_fleet_plant': kpi_plant,
            'kpi_fleet_owned': kpi_owned,
            'kpi_fleet_rented': kpi_rented,
            'kpi_fleet_total_cost': total_cost,
            'kpi_fleet_fuel_cost': fuel_cost,
            'kpi_fleet_spare_cost': spare_cost,
            'kpi_fleet_rental_cost': 0.0,
            'kpi_fleet_other_cost': other_cost,
            'fleet_site_line_ids': site_lines,
            'fleet_vehicle_line_ids': vehicle_lines,
        }

    # ── Hook into parent KPI pipeline ────────────────────────────────────────

    def _compute_kpi_data(self):
        """Override: call super then merge fleet KPIs into the returned dict."""
        data = super()._compute_kpi_data()
        fleet_data = self._compute_fleet_kpis()
        data.update(fleet_data)
        return data

    @api.model
    def _refresh_dashboard_data(self, dashboard):
        """Override: extend line_fields set to include fleet line ids."""
        data = dashboard._compute_kpi_data()
        # All One2many line fields (including fleet ones)
        line_fields = {
            'project_line_ids', 'vendor_liability_line_ids', 'sub_liability_line_ids',
            'bank_line_ids', 'bg_facility_line_ids', 'bg_project_line_ids',
            'attendance_line_ids', 'ipc_line_ids', 'inventory_line_ids',
            # fleet
            'fleet_site_line_ids', 'fleet_vehicle_line_ids',
        }
        write_vals = {}
        for fname, val in data.items():
            if fname in line_fields:
                write_vals[fname] = [(5, 0, 0)] + [(0, 0, row) for row in val]
            elif fname == 'expiring_bg_ids':
                write_vals[fname] = [(6, 0, val.ids)]
            elif hasattr(val, '_name'):
                write_vals[fname] = [(6, 0, val.ids)]
            else:
                write_vals[fname] = val
        dashboard.with_context(_refreshing_dashboard=True).write(write_vals)

    @api.onchange(
        'filter_dashboard_tab', 'filter_scope', 'filter_project_id',
        'filter_period_preset', 'filter_date_from', 'filter_date_to',
    )
    def _onchange_filters(self):
        """Override: extend line_fields set to include fleet line ids."""
        if self.filter_scope == 'single' and not self.filter_project_id:
            return
        data = self._compute_kpi_data()
        line_fields = {
            'project_line_ids', 'vendor_liability_line_ids', 'sub_liability_line_ids',
            'bank_line_ids', 'bg_facility_line_ids', 'bg_project_line_ids',
            'attendance_line_ids', 'ipc_line_ids', 'inventory_line_ids',
            # fleet
            'fleet_site_line_ids', 'fleet_vehicle_line_ids',
        }
        for fname, val in data.items():
            if fname in line_fields:
                setattr(self, fname, [(5, 0, 0)] + [(0, 0, row) for row in val])
            elif fname == 'expiring_bg_ids':
                setattr(self, fname, val)
            elif hasattr(val, '_name'):
                setattr(self, fname, val)
            else:
                setattr(self, fname, val)
