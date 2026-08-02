"""Fleet KPI extensions to x.management.dashboard."""

import logging
from datetime import date as date_cls
from dateutil.relativedelta import relativedelta
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
    last_meter = fields.Float(readonly=True, digits=(16, 2))
    meter_unit = fields.Char(readonly=True)


class ManagementDashboardFleetMonthlyLine(models.TransientModel):
    _name = 'x.management.dashboard.fleet.monthly.line'
    _description = 'Fleet Dashboard — Monthly Cost Trend'
    _order = 'period_date asc'

    dashboard_id = fields.Many2one(
        'x.management.dashboard', ondelete='cascade', required=True)
    month_label = fields.Char(readonly=True)
    period_date = fields.Date(readonly=True)
    fuel_cost = fields.Monetary(readonly=True, currency_field='currency_id')
    spare_cost = fields.Monetary(readonly=True, currency_field='currency_id')
    other_cost = fields.Monetary(readonly=True, currency_field='currency_id')
    total_cost = fields.Monetary(readonly=True, currency_field='currency_id')
    currency_id = fields.Many2one('res.currency', readonly=True)


# ── Management Dashboard extension ──────────────────────────────────────────

_FLEET_LINE_FIELDS = frozenset({
    'fleet_site_line_ids',
    'fleet_vehicle_line_ids',
    'fleet_monthly_line_ids',
})

_UNIT_LABEL = {
    'kilometers': 'km',
    'miles': 'mi',
    'hours': 'hr',
    'units': 'units',
}


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
        string='Rental Cost (Bills)', readonly=True, currency_field='currency_id')
    kpi_fleet_other_cost = fields.Monetary(
        string='Other Cost', readonly=True, currency_field='currency_id')
    kpi_fleet_owned_cost = fields.Monetary(
        string='Owned Vehicle Cost', readonly=True, currency_field='currency_id')
    kpi_fleet_rented_cost = fields.Monetary(
        string='Rented Vehicle Op. Cost', readonly=True, currency_field='currency_id')

    kpi_fleet_ltv_cost = fields.Monetary(
        string='LTV Cost', readonly=True, currency_field='currency_id')
    kpi_fleet_htv_cost = fields.Monetary(
        string='HTV Cost', readonly=True, currency_field='currency_id')
    kpi_fleet_plant_cost = fields.Monetary(
        string='Plant &amp; Equipment Cost', readonly=True, currency_field='currency_id')

    fleet_site_line_ids = fields.One2many(
        'x.management.dashboard.fleet.site.line', 'dashboard_id',
        string='Fleet Cost by Site', readonly=True)
    fleet_vehicle_line_ids = fields.One2many(
        'x.management.dashboard.fleet.vehicle.line', 'dashboard_id',
        string='Top Vehicles by Cost', readonly=True)
    fleet_monthly_line_ids = fields.One2many(
        'x.management.dashboard.fleet.monthly.line', 'dashboard_id',
        string='Monthly Cost Trend', readonly=True)

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

        # Ownership partition
        owned_vehicles = vehicles.filtered(lambda v: v.x_ownership_type == 'owned')
        rented_vehicles = vehicles.filtered(lambda v: v.x_ownership_type == 'rented')
        owned_ids = set(owned_vehicles.ids)
        rented_ids = set(rented_vehicles.ids)

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

        # ── Owned vs rented cost split ─────────────────────────────────────
        owned_log_cost = sum(
            log_entries.filtered(lambda l: l.vehicle_id.id in owned_ids).mapped('amount'))
        rented_log_cost = sum(
            log_entries.filtered(lambda l: l.vehicle_id.id in rented_ids).mapped('amount'))
        owned_spare_cost = sum(
            SparePart.search(spare_domain + [('vehicle_id', 'in', list(owned_ids))]).mapped(
                'x_total_cost')) if owned_ids else 0.0
        rented_spare_cost = sum(
            SparePart.search(spare_domain + [('vehicle_id', 'in', list(rented_ids))]).mapped(
                'x_total_cost')) if rented_ids else 0.0
        kpi_fleet_owned_cost = owned_log_cost + owned_spare_cost
        kpi_fleet_rented_cost = rented_log_cost + rented_spare_cost

        # ── Rental cost from vendor bills ──────────────────────────────────
        rental_vendor_ids = rented_vehicles.mapped('x_rental_vendor_id').filtered(
            lambda p: p.id).ids
        rental_cost = 0.0
        if rental_vendor_ids and analytic_ids:
            bill_domain = [
                ('move_type', '=', 'in_invoice'),
                ('state', '=', 'posted'),
                ('partner_id', 'in', rental_vendor_ids),
                ('x_project_analytic_account_id', 'in', analytic_ids),
            ]
            if date_from:
                bill_domain.append(('invoice_date', '>=', date_from))
            if date_to:
                bill_domain.append(('invoice_date', '<=', date_to))
            rental_bills = self.env['account.move'].search(bill_domain)
            rental_cost = sum(rental_bills.mapped('amount_untaxed'))

        # ── Per-site breakdown ─────────────────────────────────────────────
        site_lines = []
        sites = self.env['x.project.site.config'].search(
            [('analytic_account_id', 'in', analytic_ids)] if analytic_ids else [])
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

            # Rental bills scoped to this site
            site_rented = site_vehicles.filtered(
                lambda v: v.x_ownership_type == 'rented' and v.x_rental_vendor_id)
            site_rental_vendor_ids = site_rented.mapped('x_rental_vendor_id').filtered(
                lambda p: p.id).ids
            s_rental = 0.0
            if site_rental_vendor_ids:
                site_analytic_id = site.analytic_account_id.id
                site_bill_domain = [
                    ('move_type', '=', 'in_invoice'),
                    ('state', '=', 'posted'),
                    ('partner_id', 'in', site_rental_vendor_ids),
                    ('x_project_analytic_account_id', '=', site_analytic_id),
                ]
                if date_from:
                    site_bill_domain.append(('invoice_date', '>=', date_from))
                if date_to:
                    site_bill_domain.append(('invoice_date', '<=', date_to))
                site_rental_bills = self.env['account.move'].search(site_bill_domain)
                s_rental = sum(site_rental_bills.mapped('amount_untaxed'))

            s_total = s_fuel + s_other + s_spare + s_rental
            site_lines.append({
                'analytic_account_id': site.analytic_account_id.id,
                'site_name': site.name,
                'vehicle_count': len(site_vehicles),
                'fuel_cost': s_fuel,
                'spare_cost': s_spare,
                'rental_cost': s_rental,
                'other_cost': s_other,
                'total_cost': s_total,
                'currency_id': currency.id,
            })

        # ── Per-vehicle top 20 by cost ─────────────────────────────────────
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
                'last_meter': v.odometer or 0.0,
                'meter_unit': _UNIT_LABEL.get(v.odometer_unit, 'km'),
            })
        vehicle_lines.sort(key=lambda x: x['total_cost'], reverse=True)
        vehicle_lines = vehicle_lines[:20]

        total_cost = fuel_cost + other_cost + spare_cost + rental_cost

        # ── LTV / HTV / Plant cost split ───────────────────────────────────
        ltv_ids = set(vehicles.filtered(lambda v: v.x_fleet_type == 'ltv').ids)
        htv_ids = set(vehicles.filtered(lambda v: v.x_fleet_type == 'htv').ids)
        plant_ids = set(vehicles.filtered(lambda v: v.x_fleet_type == 'plant').ids)

        def _type_cost(type_ids):
            if not type_ids:
                return 0.0
            l_c = sum(
                log_entries.filtered(
                    lambda l, ids=type_ids: l.vehicle_id.id in ids
                ).mapped('amount'))
            s_c = sum(
                SparePart.search(spare_domain + [('vehicle_id', 'in', list(type_ids))]
                                 ).mapped('x_total_cost'))
            return l_c + s_c

        kpi_ltv_cost = _type_cost(ltv_ids)
        kpi_htv_cost = _type_cost(htv_ids)
        kpi_plant_cost = _type_cost(plant_ids)

        # ── Monthly cost trend — last 12 calendar months ───────────────────
        monthly_lines = []
        today_date = date_cls.today()
        for i in range(11, -1, -1):  # oldest → newest
            month_start = today_date.replace(day=1) - relativedelta(months=i)
            month_end = month_start + relativedelta(months=1) - relativedelta(days=1)
            m_logs = LogEntry.search([
                ('vehicle_id', 'in', vehicles.ids),
                ('date', '>=', month_start),
                ('date', '<=', month_end),
            ])
            m_spares = SparePart.search([
                ('vehicle_id', 'in', vehicles.ids),
                ('state', '=', 'posted'),
                ('date', '>=', month_start),
                ('date', '<=', month_end),
            ])
            m_fuel = sum(
                m_logs.filtered(
                    lambda l: l.x_log_category in fuel_cats).mapped('amount'))
            m_other = sum(
                m_logs.filtered(
                    lambda l: l.x_log_category in other_cats).mapped('amount'))
            m_spare = sum(m_spares.mapped('x_total_cost'))
            m_total = m_fuel + m_other + m_spare
            monthly_lines.append({
                'month_label': month_start.strftime('%b %Y'),
                'period_date': month_start,
                'fuel_cost': m_fuel,
                'spare_cost': m_spare,
                'other_cost': m_other,
                'total_cost': m_total,
                'currency_id': currency.id,
            })

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
            'kpi_fleet_rental_cost': rental_cost,
            'kpi_fleet_other_cost': other_cost,
            'kpi_fleet_owned_cost': kpi_fleet_owned_cost,
            'kpi_fleet_rented_cost': kpi_fleet_rented_cost,
            'kpi_fleet_ltv_cost': kpi_ltv_cost,
            'kpi_fleet_htv_cost': kpi_htv_cost,
            'kpi_fleet_plant_cost': kpi_plant_cost,
            'fleet_site_line_ids': site_lines,
            'fleet_vehicle_line_ids': vehicle_lines,
            'fleet_monthly_line_ids': monthly_lines,
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
            'fleet_site_line_ids', 'fleet_vehicle_line_ids', 'fleet_monthly_line_ids',
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
            'fleet_site_line_ids', 'fleet_vehicle_line_ids', 'fleet_monthly_line_ids',
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
