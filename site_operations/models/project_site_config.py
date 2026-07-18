from dateutil.relativedelta import relativedelta

from odoo import models, fields, api, _

# Demo / default site warehouses — one per Matracon project (code, display name).
_SITE_WAREHOUSE_DEFAULTS = {
    'MCH - BAHAWALNAGAR': ('MCH', 'MCH Site Warehouse'),
    'RWASA': ('RWASA', 'RWASA Site Warehouse'),
    'STP - MARDAN': ('STP', 'STP Site Warehouse'),
}


class ProjectSiteConfigProjectLink(models.Model):
    _inherit = 'x.project.site.config'

    project_id = fields.Many2one(
        'project.project',
        string='Odoo Project',
        readonly=True,
        copy=False,
        help='Linked native project record — financial dashboard and fund balances.',
    )

    # ── Issue Locations (auto-created under site warehouse) ───────────────────
    x_employee_location_id = fields.Many2one(
        'stock.location',
        string='Employee Issue Location',
        readonly=True,
        copy=False,
        help='Auto-created under the site warehouse view location. '
             'Materials issued to employees are tracked here (e.g. MCH/Employees).',
    )
    x_subcontractor_location_id = fields.Many2one(
        'stock.location',
        string='Subcontractor Issue Location',
        readonly=True,
        copy=False,
        help='Auto-created under the site warehouse view location. '
             'Materials issued to subcontractors are tracked here (e.g. MCH/Subcontractor).',
    )

    # ── Petty Cash Account ────────────────────────────────────────────────────
    x_petty_cash_account_id = fields.Many2one(
        'account.account',
        string='Petty Cash Account',
        help='The "Cash in Hand" GL account for this site\'s petty cash '
             '(e.g. Cash in Hand — RWASA). '
             'Set once here and it auto-fills on all petty cash expenses, '
             'fund releases, and site cash-out entries for this project.',
    )

    x_site_accountant_ids = fields.Many2many(
        'res.users',
        'x_project_site_accountant_rel',
        'config_id', 'user_id',
        string='Site Accountants',
        help=(
            'Site Accountants for this project. Adding a user here automatically assigns '
            'the Site Accountant security group and sets their default analytic account.'
        ),
    )
    x_accountant_count = fields.Integer(
        compute='_compute_accountant_count',
        string='Accountants',
    )

    # ── Project Timeline / EOT ─────────────────────────────────────────────────
    x_project_start_date = fields.Date(
        string='Project Start Date', tracking=True,
        help='Date work officially commenced on this project.')
    x_project_deadline = fields.Date(
        string='Original Project Deadline', tracking=True,
        help='Baseline contractual completion date before any extensions.')
    x_eot_ids = fields.One2many(
        'x.project.eot', 'site_config_id', string='Extension of Time History')
    x_current_deadline = fields.Date(
        string='Current Deadline',
        compute='_compute_current_deadline', store=True,
        help='Original deadline plus the cumulative duration of all approved EOTs.')

    @api.depends('x_project_deadline', 'x_eot_ids.duration_months', 'x_eot_ids.eot_date')
    def _compute_current_deadline(self):
        for rec in self:
            if not rec.x_project_deadline:
                rec.x_current_deadline = False
                continue
            running = rec.x_project_deadline
            for eot in rec.x_eot_ids.sorted(lambda e: (e.eot_date or fields.Date.today(), e.id)):
                running = running + relativedelta(months=eot.duration_months or 0)
            rec.x_current_deadline = running

    @api.depends('x_site_accountant_ids')
    def _compute_accountant_count(self):
        for config in self:
            config.x_accountant_count = len(config.x_site_accountant_ids)

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        records._ensure_project_record()
        for record in records.filtered(lambda c: not c.warehouse_id):
            record._matracon_ensure_warehouse_for_config()
        for record in records:
            # Ensure issue locations exist (idempotent — skips if already set)
            if record.warehouse_id:
                record._matracon_ensure_site_issue_locations()
            if record.site_user_ids:
                record._assign_users(record.site_user_ids)
            if record.x_site_accountant_ids:
                record._assign_accountants(record.x_site_accountant_ids)
        return records

    def write(self, vals):
        old_accountant_sets = {
            config.id: set(config.x_site_accountant_ids.ids) for config in self
        }
        res = super().write(vals)
        if 'x_site_accountant_ids' in vals:
            for config in self:
                old_users = old_accountant_sets.get(config.id, set())
                new_users = set(config.x_site_accountant_ids.ids)
                added_ids = new_users - old_users
                removed_ids = old_users - new_users
                if added_ids:
                    config._assign_accountants(
                        self.env['res.users'].browse(list(added_ids)))
                if removed_ids:
                    config._unassign_accountants(
                        self.env['res.users'].browse(list(removed_ids)))
        if any(k in vals for k in (
            'name', 'analytic_account_id', 'site_user_ids', 'x_site_accountant_ids',
        )):
            self._ensure_project_record()
        if 'name' in vals:
            self._matracon_sync_name_to_linked_records()
        if 'warehouse_id' not in vals and any(k in vals for k in ('name', 'analytic_account_id')):
            for config in self.filtered(lambda c: not c.warehouse_id):
                config._matracon_ensure_warehouse_for_config()
        if any(k in vals for k in (
            'warehouse_id', 'site_user_ids', 'x_site_accountant_ids',
        )):
            self._matracon_sync_user_operational_warehouse()
        return res

    def _matracon_sync_name_to_linked_records(self):
        """Propagate a site config rename to all linked records:
        analytic account, warehouse, and project.project (via _ensure_project_record).
        """
        for config in self:
            new_name = config.name
            if not new_name:
                continue
            # ── Analytic account ──────────────────────────────────────────────
            if config.analytic_account_id:
                config.analytic_account_id.sudo().write({'name': new_name})
            # ── Warehouse name (keep existing code — changing code is disruptive) ──
            if config.warehouse_id:
                # Build the expected warehouse name suffix
                new_wh_name = f'{new_name} Site Warehouse'
                config.warehouse_id.sudo().write({'name': new_wh_name})

    def _ensure_project_record(self):
        """Create or link project.project for each site configuration."""
        Project = self.env['project.project']
        for config in self:
            if not config.analytic_account_id:
                continue
            project = config.project_id
            if not project:
                project = Project.search(
                    [('x_analytic_account_id', '=', config.analytic_account_id.id)],
                    limit=1,
                )
            if not project:
                project = Project.create({
                    'name': config.name,
                    'x_analytic_account_id': config.analytic_account_id.id,
                })
            else:
                project.write({
                    'name': config.name,
                    'x_analytic_account_id': config.analytic_account_id.id,
                })
            config.project_id = project.id
            project.write({
                'x_site_config_id': config.id,
                'x_site_store_user_ids': [(6, 0, config.site_user_ids.ids)],
                'x_site_accountant_user_ids': [
                    (6, 0, config.x_site_accountant_ids.ids)
                ],
            })

    def _assign_users(self, users):
        super()._assign_users(users)
        for user in users:
            if self.project_id:
                user.sudo().write({'x_default_project_id': self.project_id.id})
        self._matracon_sync_user_operational_warehouse()

    def _assign_accountants(self, users):
        """Assign Site Accountant group and project defaults."""
        accountant_group = self.env.ref(
            'site_operations.group_site_accountant', raise_if_not_found=False)
        Users = self.env['res.users']
        for user in users:
            vals = {
                'x_default_analytic_account_id': self.analytic_account_id.id,
                'x_default_warehouse_id': (
                    self.warehouse_id.id if self.warehouse_id else False),
                'x_site_config_id': self.id,
            }
            user.sudo().write(vals)
            if accountant_group:
                Users._matracon_add_group(user, accountant_group)
            if self.project_id:
                user.sudo().write({'x_default_project_id': self.project_id.id})
        self._matracon_sync_user_operational_warehouse()

    def _unassign_accountants(self, users):
        """Reverse accountant assignment when removed from site config."""
        accountant_group = self.env.ref(
            'site_operations.group_site_accountant', raise_if_not_found=False)
        Users = self.env['res.users']
        for user in users:
            other_config = self.search([
                ('x_site_accountant_ids', 'in', user.id),
                ('id', '!=', self.id),
            ], limit=1)
            if other_config:
                user.sudo().write({
                    'x_default_analytic_account_id': other_config.analytic_account_id.id,
                    'x_default_warehouse_id': (
                        other_config.warehouse_id.id
                        if other_config.warehouse_id else False),
                    'x_site_config_id': other_config.id,
                    'x_default_project_id': (
                        other_config.project_id.id
                        if other_config.project_id else False),
                })
            else:
                unwrite_vals = {
                    'x_default_analytic_account_id': False,
                    'x_default_warehouse_id': False,
                    'x_site_config_id': False,
                    'x_default_project_id': False,
                }
                user.sudo().write(unwrite_vals)
                if accountant_group:
                    Users._matracon_remove_group(user, accountant_group)

    def action_open_project(self):
        self.ensure_one()
        self._ensure_project_record()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Project — %s') % self.name,
            'res_model': 'project.project',
            'view_mode': 'form',
            'res_id': self.project_id.id,
        }

    def action_view_accountants(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Site Accountants — %s') % self.name,
            'res_model': 'res.users',
            'view_mode': 'list,form',
            'domain': [('id', 'in', self.x_site_accountant_ids.ids)],
            'context': {'default_x_site_config_id': self.id},
        }

    def action_view_project_dashboard(self):
        """Open financial overview for this site."""
        self.ensure_one()
        self._ensure_project_record()
        action = self.env.ref(
            'site_operations.action_project_financial_overview').read()[0]
        action['domain'] = [('id', '=', self.project_id.id)]
        return action

    def _matracon_main_operational_warehouse(self, company):
        """Company main warehouse where stock is received and issued (usually code WH)."""
        Warehouse = self.env['stock.warehouse'].sudo()
        main = Warehouse.search([
            ('company_id', '=', company.id),
            ('code', '=', 'WH'),
        ], limit=1)
        if main:
            return main
        return Warehouse.search(
            [('company_id', '=', company.id)], order='id asc', limit=1)

    def _matracon_site_warehouse_has_stock(self, warehouse):
        if not warehouse or not warehouse.lot_stock_id:
            return False
        return bool(self.env['stock.quant'].sudo().search_count([
            ('location_id', 'child_of', warehouse.lot_stock_id.id),
            ('quantity', '>', 0),
        ]))

    def _matracon_operational_warehouse_for_config(self, company):
        """Always return the site's own warehouse.

        The previous fallback to the main company WH when the site warehouse had
        no stock caused cascading security errors: record rules scope stock.location
        reads to the site warehouse only, so reading the main WH's lot_stock_id
        (location) raised an Access Denied for site-store users.  Returning the
        site warehouse unconditionally is correct — an empty warehouse shows zero
        stock rather than leaking another site's inventory.
        """
        self.ensure_one()
        return self.warehouse_id or self._matracon_main_operational_warehouse(company)

    def _matracon_sync_user_operational_warehouse(self):
        """Keep x_default_warehouse_id aligned with the site's own warehouse."""
        company = self.env.company
        for config in self:
            op_wh = config._matracon_operational_warehouse_for_config(company)
            if not op_wh:
                continue
            users = config.site_user_ids | config.x_site_accountant_ids
            if users:
                users.sudo().write({'x_default_warehouse_id': op_wh.id})

    @api.model
    def _matracon_find_site_warehouse(self, code, name, company=None):
        """Find existing site warehouse by code or name (idempotent upgrades)."""
        company = company or self.env.company
        Warehouse = self.env['stock.warehouse'].sudo()
        wh = Warehouse.search([
            ('code', '=', code),
            ('company_id', '=', company.id),
        ], limit=1)
        if wh:
            return wh
        return Warehouse.search([
            ('name', '=', name),
            ('company_id', '=', company.id),
        ], limit=1)

    @api.model
    def _matracon_ensure_site_warehouses(self):
        """Ensure each site project config has a warehouse (demo + production)."""
        for config in self.search([]):
            config._matracon_ensure_warehouse_for_config()

    def _matracon_ensure_warehouse_for_config(self):
        """Create/link a site warehouse for one project configuration."""
        self.ensure_one()
        if self.warehouse_id:
            self._matracon_sync_user_operational_warehouse()
            self._matracon_ensure_site_issue_locations()
            return self.warehouse_id
        Warehouse = self.env['stock.warehouse'].sudo()
        company = self.env.company
        defaults = _SITE_WAREHOUSE_DEFAULTS.get(self.name)
        if defaults:
            code, name = defaults
        else:
            code = ''.join(
                part[0] for part in self.name.split() if part
            )[:5].upper() or 'SITE'
            name = f'{self.name} Site Warehouse'
        wh = self._matracon_find_site_warehouse(code, name, company)
        if not wh:
            wh = Warehouse.create({
                'name': name,
                'code': code,
                'company_id': company.id,
            })
        self.sudo().write({'warehouse_id': wh.id})
        self._matracon_sync_user_operational_warehouse()
        self._matracon_ensure_site_issue_locations()
        return wh

    def _matracon_ensure_site_issue_locations(self):
        """Create/link Employee and Subcontractor virtual locations under the site warehouse.

        Called on create and whenever a warehouse is linked.  Safe to call multiple
        times — only creates a location when the field is still empty.
        """
        self.ensure_one()
        if not self.warehouse_id or not self.warehouse_id.view_location_id:
            return
        view_loc = self.warehouse_id.view_location_id
        Location = self.env['stock.location'].sudo()
        write_vals = {}

        # ── Employee location ─────────────────────────────────────────────────
        if not self.x_employee_location_id:
            emp_loc = Location.with_context(active_test=False).search([
                ('name', '=', 'Employees'),
                ('location_id', '=', view_loc.id),
            ], limit=1)
            if not emp_loc:
                emp_loc = Location.create({
                    'name': 'Employees',
                    'location_id': view_loc.id,
                    'usage': 'internal',
                })
            elif not emp_loc.active:
                emp_loc.write({'active': True})
            write_vals['x_employee_location_id'] = emp_loc.id

        # ── Subcontractor location ────────────────────────────────────────────
        if not self.x_subcontractor_location_id:
            sub_loc = Location.with_context(active_test=False).search([
                ('name', '=', 'Subcontractor'),
                ('location_id', '=', view_loc.id),
            ], limit=1)
            if not sub_loc:
                sub_loc = Location.create({
                    'name': 'Subcontractor',
                    'location_id': view_loc.id,
                    'usage': 'internal',
                })
            elif not sub_loc.active:
                sub_loc.write({'active': True})
            write_vals['x_subcontractor_location_id'] = sub_loc.id

        if write_vals:
            self.sudo().write(write_vals)


class ProjectEot(models.Model):
    """Extension of Time (EOT) — each record extends the project deadline by a
    given number of months.  The cumulative result is shown as
    ``x_current_deadline`` on the parent site configuration."""
    _name = 'x.project.eot'
    _description = 'Project Extension of Time (EOT)'
    _order = 'eot_date asc, id asc'

    site_config_id = fields.Many2one(
        'x.project.site.config', string='Project Site',
        required=True, ondelete='cascade', index=True)
    eot_no = fields.Integer(
        string='No.', readonly=True, copy=False,
        help='Sequential EOT number within this project (auto-assigned).')
    eot_date = fields.Date(
        string='Approval Date', required=True, default=fields.Date.context_today,
        help='Date on which the extension was formally approved / issued.')
    duration_months = fields.Integer(
        string='Duration (Months)', required=True,
        help='Number of calendar months by which the deadline is extended.')
    effective_deadline = fields.Date(
        string='Effective Deadline',
        compute='_compute_effective_deadline',
        help='Cumulative project deadline after applying this and all prior EOTs.')
    notes = fields.Text(string='Notes / Reference')

    @api.depends(
        'site_config_id.x_project_deadline',
        'site_config_id.x_eot_ids.duration_months',
        'site_config_id.x_eot_ids.eot_date',
    )
    def _compute_effective_deadline(self):
        for rec in self:
            baseline = rec.site_config_id.x_project_deadline
            if not baseline:
                rec.effective_deadline = False
                continue
            running = baseline
            for eot in rec.site_config_id.x_eot_ids.sorted(
                lambda e: (e.eot_date or fields.Date.today(), e.id)
            ):
                running = running + relativedelta(months=eot.duration_months or 0)
                if eot.id == rec.id:
                    break
            rec.effective_deadline = running

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if not vals.get('eot_no') and vals.get('site_config_id'):
                existing = self.search_count(
                    [('site_config_id', '=', vals['site_config_id'])])
                vals['eot_no'] = existing + 1
        return super().create(vals_list)
