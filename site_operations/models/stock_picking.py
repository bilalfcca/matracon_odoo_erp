from datetime import date as dt_date
from dateutil.relativedelta import relativedelta

from markupsafe import Markup

from odoo import models, fields, api, _
from odoo.exceptions import UserError

from . import matracon_notifications as matracon_notify


class StockPickingSiteOps(models.Model):
    _inherit = 'stock.picking'

    # ── Transfer classification ───────────────────────────────────────────────
    x_transfer_purpose = fields.Selection([
        ('material_issuance', 'Material Issuance'),
        ('site_to_site', 'Site To Site Transfer'),
    ], string='Transfer Purpose', tracking=True)

    x_issue_type = fields.Selection([
        ('normal', 'Normal'),
        ('subcontractor', 'Subcontractor'),
    ], string='Issue Type', default='normal', tracking=True)

    x_inventory_type = fields.Selection([
        ('asset', 'Asset'),
        ('consumable', 'Consumable'),
    ], string='Inventory Type', default='consumable', tracking=True)

    # ── Contact ───────────────────────────────────────────────────────────────
    x_contact_id = fields.Many2one(
        'res.partner', string='Issuance Contact', tracking=True,
        help='Employee or subcontractor receiving the material')

    # ── Project (auto-filled from user site config) ───────────────────────────
    x_issuance_project_id = fields.Many2one(
        'account.analytic.account', string='Project',
        tracking=True, readonly=True,
        help='Auto-filled from the logged-in user site configuration')

    x_product_ids_at_location = fields.Many2many(
        'product.product',
        compute='_compute_product_ids_at_location',
        string='Products At Source Location',
        help='Products with stock at the site warehouse source location.',
    )

    # ── Gate Pass ─────────────────────────────────────────────────────────────
    x_generate_gate_pass = fields.Boolean(
        string='Generate Gate Pass Outward', default=True)
    x_gate_pass_outward_no = fields.Char(string='Gate Pass No (Outward)')

    # ── Backcharge ────────────────────────────────────────────────────────────
    x_backcharge_applicable = fields.Boolean(
        string='Backcharge Applicable', default=False, tracking=True)
    x_backcharge_amount = fields.Float(
        string='Backcharge Amount', compute='_compute_backcharge_amount',
        store=True, readonly=True,
        help='Auto-computed sum of per-line backcharge amounts')
    x_backcharge_description = fields.Text(string='Backcharge Description')
    x_backcharge_refund_entry_id = fields.Many2one(
        'account.move', string='Backcharge Entry', readonly=True,
        help='Auto-generated accounting entry on validation')

    # ── Returns ───────────────────────────────────────────────────────────────
    x_is_return_transfer = fields.Boolean(string='Is Return', default=False)
    x_original_issuance_id = fields.Many2one(
        'stock.picking', string='Original Issuance')
    x_return_type = fields.Selection([
        ('normal', 'Normal Return'),
        ('damaged', 'Damaged / Lost'),
    ], string='Return Type')
    x_return_condition = fields.Char(string='Return Condition')
    x_return_remarks = fields.Text(string='Return Remarks')
    x_return_backcharge_applicable = fields.Boolean(string='Backcharge on Return')
    x_return_backcharge_amount = fields.Float(
        string='Return Backcharge Amount', default=0.0,
        help='Manual amount to charge back to the subcontractor when returning an asset.')
    x_return_backcharge_entry_id = fields.Many2one(
        'account.move', string='Return Adjustment Entry', readonly=True)

    # ── Outstanding materials summary ─────────────────────────────────────────
    x_outstanding_materials_html = fields.Html(
        string='Outstanding Materials Summary',
        compute='_compute_outstanding_materials', store=False)

    # ── Computed qty helpers (for list view) ──────────────────────────────────
    x_original_issued_qty = fields.Float(
        string='Original Issued Qty', compute='_compute_qty_summary', store=False)
    x_total_returned_qty = fields.Float(
        string='Total Returned Qty', compute='_compute_qty_summary', store=False)
    x_outstanding_qty = fields.Float(
        string='Outstanding Qty', compute='_compute_qty_summary', store=False)

    # ── Site-to-Site ──────────────────────────────────────────────────────────
    x_dest_project_id = fields.Many2one(
        'account.analytic.account', string='Destination Project', tracking=True,
        help='Only for Site To Site transfers')
    x_site_transfer_state = fields.Selection([
        ('draft', 'Draft'),
        ('pending_approval', 'Pending Approval'),
        ('approved', 'Approved'),
        ('rejected', 'Rejected'),
        ('done', 'Done'),
    ], string='Transfer Status', default='draft', tracking=True, copy=False)
    x_is_dest_receipt = fields.Boolean(
        string='Destination Receipt', default=False, copy=False,
        help='Incoming transfer at the destination site store.')
    x_source_transfer_id = fields.Many2one(
        'stock.picking', string='Source Transfer', readonly=True, copy=False)
    x_dest_picking_id = fields.Many2one(
        'stock.picking', string='Destination Receipt', readonly=True, copy=False)
    x_interproject_entry_id = fields.Many2one(
        'account.move', string='Inter-Project Entry', readonly=True)
    x_damage_backcharge_entry_id = fields.Many2one(
        'account.move', string='Damage Backcharge Entry', readonly=True)

    # ── User context flags (for view domains) ────────────────────────────────
    x_is_site_store = fields.Boolean(
        string='Is Site Store User',
        compute='_compute_x_is_site_store',
        store=False,
    )

    # ── Site project fields — used for issuance project dropdown + location domain ──
    # Analytic accounts for every site config the current user belongs to.
    x_user_site_analytic_ids = fields.Many2many(
        'account.analytic.account',
        compute='_compute_user_site_fields',
        string='User Site Analytics',
    )
    # True when user belongs to more than one site — unlocks the project dropdown.
    x_user_has_multi_site = fields.Boolean(
        compute='_compute_user_site_fields',
    )
    # Parent view-location of this picking's warehouse — used to restrict
    # the Destination Location dropdown on incoming receipts for site store users.
    x_site_wh_view_location_id = fields.Many2one(
        'stock.location',
        compute='_compute_site_wh_view_location',
        string='Site WH View Location',
    )

    # ── Backcharge records auto-created on return validation ─────────────────
    x_employee_backcharge_id = fields.Many2one(
        'x.employee.backcharge', string='Employee Backcharge',
        readonly=True, copy=False,
        help='Auto-created when return with damage is validated for an employee.')
    x_sub_backcharge_id = fields.Many2one(
        'x.subcontractor.backcharge', string='Subcontractor Backcharge',
        readonly=True, copy=False,
        help='Auto-created when return with damage is validated for a subcontractor.')

    # ── Smart button counts ───────────────────────────────────────────────────
    x_return_count = fields.Integer(
        string='Return Transfers', compute='_compute_x_return_count', store=False)
    x_backcharge_entry_count = fields.Integer(
        string='Backcharge Entries', compute='_compute_entry_counts', store=False)
    x_interproject_entry_count = fields.Integer(
        string='Inter-Project Entries', compute='_compute_entry_counts', store=False)
    x_employee_backcharge_count = fields.Integer(
        compute='_compute_return_backcharge_counts', store=False)
    x_sub_backcharge_count = fields.Integer(
        compute='_compute_return_backcharge_counts', store=False)

    # ─────────────────────────────────────────────────────────────────────────
    # COMPUTE
    # ─────────────────────────────────────────────────────────────────────────

    def _compute_x_is_site_store(self):
        is_store = self.env.user.has_group('purchase_demand_raise.group_site_store')
        for pick in self:
            pick.x_is_site_store = is_store

    def _compute_user_site_fields(self):
        """Analytic accounts for the current user's site projects (for dropdown/domain)."""
        user = self.env.user
        if user.has_group('purchase_demand_raise.group_site_store'):
            configs = self.env['x.project.site.config'].sudo().search([
                ('site_user_ids', 'in', user.id),
            ])
            analytic_ids = configs.mapped('analytic_account_id').filtered(bool).ids
            has_multi = len(analytic_ids) > 1
        else:
            analytic_ids = []
            has_multi = False
        for pick in self:
            pick.x_user_site_analytic_ids = [(6, 0, analytic_ids)]
            pick.x_user_has_multi_site = has_multi

    @api.depends('picking_type_id')
    def _compute_site_wh_view_location(self):
        """View-level parent location of this picking's warehouse.
        Used to restrict location_dest_id on incoming receipts to the project warehouse.
        """
        is_site_store = self.env.user.has_group('purchase_demand_raise.group_site_store')
        for pick in self:
            wh = pick.picking_type_id.warehouse_id if pick.picking_type_id else False
            if is_site_store and wh and wh.view_location_id:
                pick.x_site_wh_view_location_id = wh.view_location_id
            else:
                pick.x_site_wh_view_location_id = False

    # ─────────────────────────────────────────────────────────────────────────
    # SITE WAREHOUSE / PRODUCT FILTERING
    # ─────────────────────────────────────────────────────────────────────────

    def _get_site_warehouse(self):
        """Warehouse for the site store user or issuance project."""
        self.ensure_one()
        user = self.env.user
        warehouse = (
            user.x_default_warehouse_id
            if hasattr(user, 'x_default_warehouse_id') else False
        )
        if not warehouse and self.x_issuance_project_id:
            config = self.env['x.project.site.config'].sudo().search([
                ('analytic_account_id', '=', self.x_issuance_project_id.id),
            ], limit=1)
            warehouse = config.warehouse_id
        return warehouse

    def _get_site_stock_location(self):
        """Main stock location for material issuance at the site warehouse."""
        warehouse = self._get_site_warehouse()
        if warehouse and warehouse.lot_stock_id:
            return warehouse.lot_stock_id
        return self.env['stock.location']

    @api.model
    def _matracon_site_stock_location_id(self, vals=None):
        """Resolve site stock location id for default_get / create."""
        user = self.env.user
        warehouse = False
        analytic_id = (vals or {}).get('x_issuance_project_id')
        if analytic_id:
            config = self.env['x.project.site.config'].sudo().search([
                ('analytic_account_id', '=', analytic_id),
            ], limit=1)
            warehouse = config.warehouse_id
        if not warehouse and hasattr(user, 'x_default_warehouse_id'):
            warehouse = user.x_default_warehouse_id
        if warehouse and warehouse.lot_stock_id:
            return warehouse.lot_stock_id.id
        return False

    @api.depends('location_id', 'x_transfer_purpose', 'x_issuance_project_id')
    def _compute_product_ids_at_location(self):
        Quant = self.env['stock.quant'].sudo()
        for pick in self:
            if pick.x_transfer_purpose != 'material_issuance' or not pick.location_id:
                pick.x_product_ids_at_location = [(5, 0, 0)]
                continue
            quants = Quant.search([
                ('location_id', 'child_of', pick.location_id.id),
                ('quantity', '>', 0),
            ])
            pick.x_product_ids_at_location = [(6, 0, quants.mapped('product_id').ids)]

    # ─────────────────────────────────────────────────────────────────────────
    # DEFAULT GET  (called when a new form is opened)
    # ─────────────────────────────────────────────────────────────────────────

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        if res.get('x_transfer_purpose') in ('material_issuance', 'site_to_site'):
            if not res.get('scheduled_date'):
                res['scheduled_date'] = fields.Datetime.now()
            user = self.env.user
            pt = None
            if hasattr(user, 'x_default_warehouse_id') and user.x_default_warehouse_id:
                pt = user.x_default_warehouse_id.int_type_id
            if not pt:
                # Use active_test=False — internal picking types may be archived
                warehouse = self.env['stock.warehouse'].search(
                    [('company_id', '=', self.env.company.id)], limit=1)
                if warehouse:
                    pt = warehouse.int_type_id
            if not pt:
                pt = self.env['stock.picking.type'].with_context(active_test=False).search(
                    [('code', '=', 'internal')], limit=1)
            if pt and not res.get('picking_type_id'):
                res['picking_type_id'] = pt.id
                if pt.default_location_dest_id:
                    res.setdefault('location_dest_id', pt.default_location_dest_id.id)
            # Material issuance: auto-fill project + source location
            if res.get('x_transfer_purpose') == 'material_issuance':
                # Auto-fill project from user's default analytic account
                if not res.get('x_issuance_project_id'):
                    if hasattr(user, 'x_default_analytic_account_id') and user.x_default_analytic_account_id:
                        res['x_issuance_project_id'] = user.x_default_analytic_account_id.id
                site_loc_id = self._matracon_site_stock_location_id(res)
                if site_loc_id:
                    res['location_id'] = site_loc_id
            elif pt and pt.default_location_src_id:
                res.setdefault('location_id', pt.default_location_src_id.id)
            # Fallback: use warehouse's main stock location (lot_stock_id)
            if not res.get('location_id'):
                if hasattr(user, 'x_default_warehouse_id') and user.x_default_warehouse_id:
                    wh_stock = user.x_default_warehouse_id.lot_stock_id
                    if wh_stock:
                        res['location_id'] = wh_stock.id
                if not res.get('location_id'):
                    warehouse = self.env['stock.warehouse'].search(
                        [('company_id', '=', self.env.company.id)], limit=1)
                    if warehouse and warehouse.lot_stock_id:
                        res['location_id'] = warehouse.lot_stock_id.id

            if res.get('x_transfer_purpose') == 'material_issuance':
                if not res.get('x_is_return_transfer'):
                    # Forward issuance: destination = Employees or Subcontractor
                    # Always force-set (picking type default for internal = stock loc)
                    issue_loc_id = self._matracon_site_issue_location_id(res)
                    if issue_loc_id:
                        res['location_dest_id'] = issue_loc_id
                    elif not res.get('location_dest_id'):
                        # Fallback to generic customer location only if nothing else was set
                        customer_loc = self.env['stock.location'].search([
                            ('usage', '=', 'customer'),
                            ('company_id', 'in', [False, self.env.company.id]),
                        ], limit=1)
                        if customer_loc:
                            res['location_dest_id'] = customer_loc.id
                else:
                    # Return: source = issue location (Employees/Sub), dest = site stock
                    issue_loc_id = self._matracon_site_issue_location_id(res)
                    stock_loc_id = self._matracon_site_stock_location_id(res)
                    if issue_loc_id:
                        res['location_id'] = issue_loc_id
                    if stock_loc_id:
                        res['location_dest_id'] = stock_loc_id
            res.setdefault('x_generate_gate_pass', True)
            # Assets never get auto-backcharge on issuance; only consumables do
            if (res.get('x_issue_type') == 'subcontractor'
                    and res.get('x_inventory_type') != 'asset'):
                res.setdefault('x_backcharge_applicable', True)
        return res

    # ─────────────────────────────────────────────────────────────────────────
    # ONCHANGE
    # ─────────────────────────────────────────────────────────────────────────

    @api.onchange('x_generate_gate_pass')
    def _onchange_generate_gate_pass(self):
        """Auto-generate a sequential gate pass number when checkbox is ticked."""
        if self.x_generate_gate_pass and not self.x_gate_pass_outward_no:
            self.x_gate_pass_outward_no = self.env['ir.sequence'].next_by_code(
                'x.gate.pass.outward') or '/'

    @api.onchange('x_issue_type', 'x_inventory_type')
    def _onchange_issue_type_backcharge(self):
        if self.x_transfer_purpose != 'material_issuance':
            return
        # Assets never carry backcharge on issuance — it is handled at return time
        if self.x_issue_type == 'subcontractor' and self.x_inventory_type != 'asset':
            self.x_backcharge_applicable = True
        else:
            self.x_backcharge_applicable = False

    @api.onchange('x_contact_id', 'x_issuance_project_id', 'move_ids', 'move_ids.product_id')
    def _onchange_contact_outstanding_preview(self):
        """Refresh outstanding materials summary live in the form."""
        self._compute_outstanding_materials()

    @api.onchange('x_contact_id', 'x_issue_type')
    def _onchange_return_auto_load(self):
        """Auto-load outstanding items when Contact + Issue Type are set on a return.

        No button click needed — selecting the contact immediately populates all
        outstanding issued items for that contact on this project/type.
        Locations are automatically reversed (from issuance dest → back to site stock).
        """
        if not self.x_is_return_transfer:
            return
        if not self.x_contact_id or not self.x_issue_type:
            # Clear lines when contact is removed
            self.move_ids = [(5, 0, 0)]
            return

        # Resolve project — use user default if not explicitly set
        project = self.x_issuance_project_id
        if not project:
            user = self.env.user
            if hasattr(user, 'x_default_analytic_account_id') and user.x_default_analytic_account_id:
                project = user.x_default_analytic_account_id
        if not project:
            return

        # ── Find all done issuances for this contact/project/type ─────────────
        domain = [
            ('x_transfer_purpose', '=', 'material_issuance'),
            ('x_contact_id', '=', self.x_contact_id.id),
            ('x_issuance_project_id', '=', project.id),
            ('x_issue_type', '=', self.x_issue_type),
            ('state', '=', 'done'),
            ('x_is_return_transfer', '=', False),
        ]
        if self.x_inventory_type:
            domain.append(('x_inventory_type', '=', self.x_inventory_type))
        issuances = self.env['stock.picking'].sudo().search(domain, order='scheduled_date desc')
        if not issuances:
            self.move_ids = [(5, 0, 0)]
            return

        # ── Set return locations from site config (not reversing old issuance) ──
        # Always use the canonical site locations so old issuances with wrong
        # destination don't propagate the error into returns.
        ref = issuances[0]
        self.picking_type_id = ref.picking_type_id

        issue_loc = self._get_site_issue_location()   # MCH/Employees or MCH/Subcontractor
        stock_loc = self._get_site_stock_location()    # MCH/Stock

        if issue_loc and stock_loc:
            src_loc = issue_loc   # return FROM employees/sub
            dest_loc = stock_loc  # return TO stock
        else:
            # Fallback: reverse original issuance locations
            src_loc = ref.location_dest_id
            dest_loc = ref.location_id

        self.location_id = src_loc
        self.location_dest_id = dest_loc

        # ── Aggregate total issued per product ────────────────────────────────
        issued_data = {}
        for iss in issuances:
            for move in iss.move_ids.filtered(lambda m: m.state == 'done' and m.product_id):
                key = (move.product_id.id, move.product_uom.id)
                if key not in issued_data:
                    issued_data[key] = {
                        'product': move.product_id,
                        'uom': move.product_uom,
                        'qty': 0.0,
                    }
                issued_data[key]['qty'] += move.quantity

        # ── Aggregate previously returned (done returns for this contact) ──────
        prev_returns = self.env['stock.picking'].sudo().search([
            ('x_is_return_transfer', '=', True),
            ('x_contact_id', '=', self.x_contact_id.id),
            ('x_issuance_project_id', '=', project.id),
            ('x_issue_type', '=', self.x_issue_type),
            ('state', '=', 'done'),
        ])
        returned_data = {}
        for ret in prev_returns:
            for move in ret.move_ids.filtered(lambda m: m.state == 'done' and m.product_id):
                pid = move.product_id.id
                returned_data[pid] = returned_data.get(pid, 0.0) + move.quantity

        # ── Build move commands for outstanding items ──────────────────────────
        new_line_cmds = [(5, 0, 0)]  # clear existing

        for (pid, uid), data in issued_data.items():
            total_issued = data['qty']
            already_returned = returned_data.get(pid, 0.0)
            outstanding = total_issued - already_returned
            if outstanding > 0.001:
                new_line_cmds.append((0, 0, {
                    'product_id': pid,
                    'product_uom_qty': outstanding,
                    'quantity': outstanding,
                    'product_uom': uid,
                    'location_id': src_loc.id,
                    'location_dest_id': dest_loc.id,
                    'x_unit_cost': data['product'].standard_price,
                    'x_issued_qty': total_issued,
                    'x_prev_returned_qty': already_returned,
                    'x_outstanding_return_qty': outstanding,
                }))

        self.move_ids = new_line_cmds

    @api.onchange('x_issuance_project_id', 'x_transfer_purpose')
    def _onchange_issuance_project_location(self):
        """Set source location from site warehouse when project is known.
        Also refreshes the destination to match the current issue type.
        """
        if self.x_transfer_purpose != 'material_issuance' or self.x_is_return_transfer:
            return
        loc = self._get_site_stock_location()
        if loc:
            self.location_id = loc
            warehouse = self._get_site_warehouse()
            if warehouse and warehouse.int_type_id:
                self.picking_type_id = warehouse.int_type_id
        # Also update destination to site-specific issue location
        issue_loc = self._get_site_issue_location()
        if issue_loc:
            self.location_dest_id = issue_loc

    @api.onchange('picking_type_id', 'x_transfer_purpose')
    def _onchange_site_ops_picking_type(self):
        """Ensure source/dest locations are set for material issuance forms."""
        if self.x_transfer_purpose not in ('material_issuance', 'site_to_site'):
            return
        if self.x_is_return_transfer:
            # Locations for returns are set by _onchange_return_auto_load
            if not self.scheduled_date:
                self.scheduled_date = fields.Datetime.now()
            return
        if self.x_transfer_purpose == 'material_issuance':
            loc = self._get_site_stock_location()
            if loc:
                self.location_id = loc
        elif self.picking_type_id:
            if self.picking_type_id.default_location_src_id:
                self.location_id = self.picking_type_id.default_location_src_id
            if (self.picking_type_id.default_location_dest_id
                    and self.x_transfer_purpose == 'material_issuance'):
                self.location_dest_id = self.picking_type_id.default_location_dest_id
        if not self.scheduled_date:
            self.scheduled_date = fields.Datetime.now()

    @api.onchange('x_contact_id', 'x_issue_type')
    def _onchange_contact_destination(self):
        """Set delivery destination for material issuance based on issue type and contact.
        Returns skip this — their locations are managed by _onchange_return_auto_load.
        """
        if self.x_transfer_purpose != 'material_issuance' or self.x_is_return_transfer:
            return

        # 1st priority: site-specific issue location (Employees / Subcontractor)
        loc = self._get_site_issue_location()
        if loc:
            self.location_dest_id = loc
            return

        # 2nd priority: type-specific virtual location (name-based fallback)
        loc = False
        if self.x_issue_type == 'normal':
            loc = self.env['stock.location'].search([
                ('usage', 'in', ('customer', 'internal')),
                ('name', 'ilike', 'employee'),
            ], limit=1)
        elif self.x_issue_type == 'subcontractor':
            loc = self.env['stock.location'].search([
                ('usage', 'in', ('customer', 'internal')),
                ('name', 'ilike', 'subcontractor'),
            ], limit=1)
        if loc:
            self.location_dest_id = loc
            return

        # 3rd priority: generic customer location (last resort fallback)
        customer_loc = self.env['stock.location'].search([
            ('usage', '=', 'customer'),
            ('company_id', 'in', [False, self.env.company.id]),
        ], limit=1)
        if customer_loc:
            self.location_dest_id = customer_loc

    @api.onchange('x_dest_project_id', 'x_transfer_purpose')
    def _onchange_site_to_site_locations(self):
        """Route site-to-site transfers through transit between warehouses."""
        if self.x_transfer_purpose != 'site_to_site' or self.x_is_dest_receipt:
            return
        user = self.env.user
        if user.x_default_warehouse_id and user.x_default_warehouse_id.lot_stock_id:
            self.location_id = user.x_default_warehouse_id.lot_stock_id
        transit = self._get_transit_location()
        if transit:
            self.location_dest_id = transit

    @api.model
    def _get_transit_location(self):
        loc = self.env.ref('stock.stock_location_inter_wh', raise_if_not_found=False)
        if not loc:
            loc = self.env['stock.location'].search(
                [('usage', '=', 'transit'), ('company_id', 'in', [False, self.env.company.id])],
                limit=1,
            )
        return loc

    def _get_site_config_for_analytic(self, analytic_account):
        if not analytic_account:
            return self.env['x.project.site.config']
        return self.env['x.project.site.config'].sudo().search(
            [('analytic_account_id', '=', analytic_account.id)], limit=1)

    def _get_site_issue_location(self, issue_type=None):
        """Return the site-specific issue location (Employees or Subcontractor).

        Lazily creates the locations if they don't exist yet on the site config,
        then re-reads the record from DB so cached-empty values are refreshed.
        """
        self.ensure_one()
        config = self._get_site_config_for_analytic(self.x_issuance_project_id)
        if not config:
            return self.env['stock.location']
        # Lazy-ensure locations exist (idempotent, no-op after first run)
        if not config.x_employee_location_id or not config.x_subcontractor_location_id:
            config._matracon_ensure_site_issue_locations()
            # Invalidate ORM cache so the just-written location ids are visible
            config.invalidate_recordset(['x_employee_location_id', 'x_subcontractor_location_id'])
        iss_type = issue_type or self.x_issue_type or 'normal'
        if iss_type == 'subcontractor':
            return config.x_subcontractor_location_id
        return config.x_employee_location_id

    @api.model
    def _matracon_site_issue_location_id(self, vals=None):
        """Resolve the site issue location id for use in default_get / create."""
        vals = vals or {}
        analytic_id = vals.get('x_issuance_project_id')
        issue_type = vals.get('x_issue_type', 'normal')
        config = None
        if analytic_id:
            config = self.env['x.project.site.config'].sudo().search(
                [('analytic_account_id', '=', analytic_id)], limit=1)
        if not config:
            user = self.env.user
            if hasattr(user, 'x_site_config_id') and user.x_site_config_id:
                config = user.x_site_config_id.sudo()
        if not config:
            return False
        # Lazy-ensure locations exist; invalidate cache so new ids are visible
        if not config.x_employee_location_id or not config.x_subcontractor_location_id:
            config._matracon_ensure_site_issue_locations()
            config.invalidate_recordset(['x_employee_location_id', 'x_subcontractor_location_id'])
        if issue_type == 'subcontractor':
            return config.x_subcontractor_location_id.id or False
        return config.x_employee_location_id.id or False

    def _get_outstanding_qty(self, product, contact, project, exclude_picking=None):
        """Qty still outstanding for product/contact on this project."""
        iss_domain = [
            ('x_transfer_purpose', '=', 'material_issuance'),
            ('x_contact_id', '=', contact.id),
            ('x_issuance_project_id', '=', project.id),
            ('state', '=', 'done'),
            ('x_is_return_transfer', '=', False),
        ]
        if exclude_picking and isinstance(exclude_picking.id, int):
            iss_domain.append(('id', '!=', exclude_picking.id))
        issuances = self.search(iss_domain)
        issued = sum(
            m.quantity for iss in issuances for m in iss.move_ids
            if m.product_id == product and m.state == 'done'
        )
        returned = sum(
            m.quantity for ret in self.search([
                ('x_original_issuance_id', 'in', issuances.ids),
                ('state', '=', 'done'),
            ]) for m in ret.move_ids
            if m.product_id == product and m.state == 'done'
        )
        return max(issued - returned, 0.0)

    # ─────────────────────────────────────────────────────────────────────────
    # CREATE
    # ─────────────────────────────────────────────────────────────────────────

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('x_transfer_purpose') in ('material_issuance', 'site_to_site'):
                user = self.env.user
                if vals.get('x_transfer_purpose') == 'material_issuance':
                    vals.setdefault('x_generate_gate_pass', True)
                    if vals.get('x_issue_type') == 'subcontractor':
                        vals.setdefault('x_backcharge_applicable', True)
                # Auto-fill project
                if not vals.get('x_issuance_project_id') and user.x_default_analytic_account_id:
                    vals['x_issuance_project_id'] = user.x_default_analytic_account_id.id
                # Auto-fill picking type + locations if missing
                if not vals.get('picking_type_id'):
                    pt = None
                    if hasattr(user, 'x_default_warehouse_id') and user.x_default_warehouse_id:
                        pt = user.x_default_warehouse_id.int_type_id
                    if not pt:
                        # Use active_test=False — internal picking types may be archived
                        warehouse = self.env['stock.warehouse'].search(
                            [('company_id', '=', self.env.company.id)], limit=1)
                        if warehouse:
                            pt = warehouse.int_type_id
                    if not pt:
                        pt = self.env['stock.picking.type'].with_context(active_test=False).search(
                            [('code', '=', 'internal')], limit=1)
                    if pt:
                        vals['picking_type_id'] = pt.id
                        if vals.get('x_transfer_purpose') != 'material_issuance':
                            vals.setdefault('location_id',
                                            pt.default_location_src_id.id if pt.default_location_src_id else False)
                        vals.setdefault('location_dest_id',
                                        pt.default_location_dest_id.id if pt.default_location_dest_id else False)
                if (vals.get('x_transfer_purpose') == 'material_issuance'
                        and not vals.get('x_is_return_transfer')):
                    site_loc_id = self._matracon_site_stock_location_id(vals)
                    if site_loc_id:
                        vals['location_id'] = site_loc_id
                # Fallback: use warehouse's main stock location (lot_stock_id)
                if not vals.get('location_id'):
                    if hasattr(user, 'x_default_warehouse_id') and user.x_default_warehouse_id:
                        wh_stock = user.x_default_warehouse_id.lot_stock_id
                        if wh_stock:
                            vals['location_id'] = wh_stock.id
                    if not vals.get('location_id'):
                        warehouse = self.env['stock.warehouse'].search(
                            [('company_id', '=', self.env.company.id)], limit=1)
                        if warehouse and warehouse.lot_stock_id:
                            vals['location_id'] = warehouse.lot_stock_id.id

                if (vals.get('x_transfer_purpose') == 'material_issuance'
                        and not vals.get('x_is_return_transfer')):
                    # Always force site-specific destination — overrides picking type default
                    issue_loc_id = self._matracon_site_issue_location_id(vals)
                    if issue_loc_id:
                        vals['location_dest_id'] = issue_loc_id
                    elif not vals.get('location_dest_id'):
                        # Fallback to generic customer location
                        customer_loc = self.env['stock.location'].search([
                            ('usage', '=', 'customer'),
                            ('company_id', 'in', [False, self.env.company.id]),
                        ], limit=1)
                        if customer_loc:
                            vals['location_dest_id'] = customer_loc.id
                if (vals.get('x_transfer_purpose') == 'site_to_site'
                        and not vals.get('x_is_dest_receipt')
                        and vals.get('x_dest_project_id')
                        and not vals.get('location_dest_id')):
                    transit = self._get_transit_location()
                    if transit:
                        vals['location_dest_id'] = transit.id
        return super().create(vals_list)

    # ─────────────────────────────────────────────────────────────────────────
    # COMPUTE METHODS
    # ─────────────────────────────────────────────────────────────────────────

    @api.depends(
        'picking_type_id', 'partner_id',
        'x_transfer_purpose', 'x_issue_type',
        'x_issuance_project_id', 'x_is_return_transfer',
    )
    def _compute_location_id(self):
        """Extend base compute — for material issuances use site-specific locations.

        Base Odoo sets both locations to the picking type's defaults whenever
        picking_type_id changes.  For internal picking types that means both
        location_id and location_dest_id become the site stock (e.g. MCH/Stock).
        We override here so that:
          • Forward issuance → source = site stock, dest = Employees / Subcontractor
          • Return           → source = Employees / Subcontractor, dest = site stock
        """
        super()._compute_location_id()
        for picking in self:
            if picking.x_transfer_purpose != 'material_issuance':
                continue
            if picking.state in ('done', 'cancel') or picking.return_id:
                continue
            if not picking.x_is_return_transfer:
                # Forward issuance: destination must be the issue location
                issue_loc = picking._get_site_issue_location()
                if issue_loc:
                    picking.location_dest_id = issue_loc
                stock_loc = picking._get_site_stock_location()
                if stock_loc:
                    picking.location_id = stock_loc
            else:
                # Return: source = issue location (Employees/Sub), dest = site stock
                issue_loc = picking._get_site_issue_location()
                stock_loc = picking._get_site_stock_location()
                if issue_loc and stock_loc:
                    picking.location_id = issue_loc
                    picking.location_dest_id = stock_loc

    @api.depends('move_ids.x_line_backcharge_amount')
    def _compute_backcharge_amount(self):
        for pick in self:
            pick.x_backcharge_amount = sum(
                pick.move_ids.mapped('x_line_backcharge_amount'))

    @api.depends('x_contact_id', 'x_issuance_project_id', 'move_ids.product_id')
    def _compute_outstanding_materials(self):
        for pick in self:
            if not pick.x_contact_id or not pick.x_issuance_project_id:
                pick.x_outstanding_materials_html = ''
                continue
            # All done issuances for this contact on this project (exclude current)
            origin_id = pick._origin.id if hasattr(pick, '_origin') and pick._origin else pick.id
            domain = [
                ('x_transfer_purpose', '=', 'material_issuance'),
                ('x_contact_id', '=', pick.x_contact_id.id),
                ('x_issuance_project_id', '=', pick.x_issuance_project_id.id),
                ('state', '=', 'done'),
                ('x_is_return_transfer', '=', False),
            ]
            if origin_id:
                domain.append(('id', '!=', origin_id))
            issuances = self.env['stock.picking'].search(domain)

            # Aggregate issued qty by product
            issued = {}
            for iss in issuances:
                for move in iss.move_ids.filtered(lambda m: m.state == 'done'):
                    key = (move.product_id.id,
                           move.product_id.display_name,
                           move.product_uom.name)
                    issued[key] = issued.get(key, 0.0) + move.quantity

            # Subtract returned qty
            returns = self.env['stock.picking'].search([
                ('x_original_issuance_id', 'in', issuances.ids),
                ('state', '=', 'done'),
            ])
            returned = {}
            for ret in returns:
                for move in ret.move_ids.filtered(lambda m: m.state == 'done'):
                    key = (move.product_id.id,
                           move.product_id.display_name,
                           move.product_uom.name)
                    returned[key] = returned.get(key, 0.0) + move.quantity

            # Build HTML table
            lines = []
            for (pid, pname, uom), qty in issued.items():
                outstanding = qty - returned.get((pid, pname, uom), 0.0)
                if outstanding > 0:
                    lines.append(
                        f'<tr>'
                        f'<td style="padding:4px 8px;">{pname}</td>'
                        f'<td style="padding:4px 8px; text-align:right;">{outstanding:,.2f}</td>'
                        f'<td style="padding:4px 8px;">{uom}</td>'
                        f'</tr>'
                    )
            if lines:
                header = (
                    '<table style="width:100%; border-collapse:collapse; '
                    'font-size:13px; border:1px solid #dee2e6;">'
                    '<thead><tr style="background:#f8f9fa;">'
                    '<th style="padding:4px 8px; text-align:left;">Product</th>'
                    '<th style="padding:4px 8px; text-align:right;">Outstanding Qty</th>'
                    '<th style="padding:4px 8px; text-align:left;">UoM</th>'
                    '</tr></thead><tbody>'
                )
                PREVIEW_ROWS = 2
                visible = lines[:PREVIEW_ROWS]
                hidden_count = len(lines) - PREVIEW_ROWS
                body = ''.join(visible)
                if hidden_count > 0:
                    body += (
                        '<tr><td colspan="3" style="padding:4px 8px; '
                        'color:#6c757d; font-style:italic; '
                        'border-top:1px dashed #dee2e6;">'
                        f'+ {hidden_count} more item(s) — see <b>Site Operations</b> tab for full list'
                        '</td></tr>'
                    )
                pick.x_outstanding_materials_html = (
                    header + body + '</tbody></table>'
                )
            else:
                pick.x_outstanding_materials_html = (
                    '<p style="color:#6c757d;">No outstanding materials for this contact.</p>'
                )

    @api.depends('move_ids.quantity', 'x_original_issuance_id', 'x_is_return_transfer')
    def _compute_qty_summary(self):
        for pick in self:
            if pick.x_is_return_transfer and pick.x_original_issuance_id:
                orig = pick.x_original_issuance_id
                pick.x_original_issued_qty = sum(orig.move_ids.mapped('quantity'))
                if isinstance(orig.id, int) and orig.id:
                    all_returns = self.search([
                        ('x_original_issuance_id', '=', orig.id),
                        ('state', '=', 'done'),
                    ])
                    pick.x_total_returned_qty = sum(
                        all_returns.mapped('move_ids').mapped('quantity'))
                else:
                    pick.x_total_returned_qty = 0.0
                pick.x_outstanding_qty = (
                    pick.x_original_issued_qty - pick.x_total_returned_qty)
            elif not pick.x_is_return_transfer:
                pick.x_original_issued_qty = sum(pick.move_ids.mapped('quantity'))
                if isinstance(pick.id, int) and pick.id:
                    all_returns = self.search([
                        ('x_original_issuance_id', '=', pick.id),
                        ('state', '=', 'done'),
                    ])
                    pick.x_total_returned_qty = sum(
                        all_returns.mapped('move_ids').mapped('quantity'))
                else:
                    pick.x_total_returned_qty = 0.0
                pick.x_outstanding_qty = (
                    pick.x_original_issued_qty - pick.x_total_returned_qty)
            else:
                pick.x_original_issued_qty = 0.0
                pick.x_total_returned_qty = 0.0
                pick.x_outstanding_qty = 0.0

    def _compute_x_return_count(self):
        for pick in self:
            if not isinstance(pick.id, int) or not pick.id:
                pick.x_return_count = 0
                continue
            pick.x_return_count = self.search_count([
                ('x_original_issuance_id', '=', pick.id),
            ])

    def _compute_entry_counts(self):
        for pick in self:
            pick.x_backcharge_entry_count = (
                1 if pick.x_backcharge_refund_entry_id else 0
            ) + (
                1 if pick.x_return_backcharge_entry_id else 0
            ) + (
                1 if pick.x_damage_backcharge_entry_id else 0
            )
            pick.x_interproject_entry_count = (
                1 if pick.x_interproject_entry_id else 0
            )

    def _compute_return_backcharge_counts(self):
        for pick in self:
            pick.x_employee_backcharge_count = 1 if pick.x_employee_backcharge_id else 0
            pick.x_sub_backcharge_count = 1 if pick.x_sub_backcharge_id else 0

    # ─────────────────────────────────────────────────────────────────────────
    # SITE-TO-SITE APPROVAL
    # ─────────────────────────────────────────────────────────────────────────

    def _check_site_transfer_approver(self):
        """Only CEO, Procurement HO, or Matracon Admin may approve/reject."""
        if self.env.su:
            return
        approver_groups = (
            'purchase_demand_raise.group_procurement_ho',
            'purchase_demand_raise.group_ceo_approval',
            'purchase_demand_raise.group_matracon_admin',
        )
        if not any(self.env.user.has_group(g) for g in approver_groups):
            raise UserError(_(
                'Only Procurement HO, CEO, or Matracon Admin can approve '
                'site-to-site transfers.'
            ))

    def action_submit_site_transfer(self):
        """Source site submits MTN for CEO / Procurement HO approval."""
        for pick in self:
            if pick.x_transfer_purpose != 'site_to_site' or pick.x_is_dest_receipt:
                raise UserError(_('Only outbound site-to-site transfers can be submitted.'))
            if not pick.move_ids:
                raise UserError(_('Add at least one product line before submitting.'))
            if not pick.x_dest_project_id:
                raise UserError(_('Select the destination project.'))
            if pick.x_dest_project_id == pick.x_issuance_project_id:
                raise UserError(_('Source and destination project must be different.'))
            if pick.x_site_transfer_state not in ('draft', 'rejected'):
                raise UserError(_('This transfer has already been submitted.'))
            pick.x_site_transfer_state = 'pending_approval'
            pick.message_post(
                body=Markup(_('Material Transfer Note submitted for approval by <b>%s</b>.')) % (
                    self.env.user.name))

    def action_approve_site_transfer(self):
        """Approve and auto-validate the outbound site-to-site transfer."""
        self._check_site_transfer_approver()
        to_validate = self.env['stock.picking']
        for pick in self.filtered(
            lambda p: p.x_transfer_purpose == 'site_to_site' and not p.x_is_dest_receipt
        ):
            if pick.x_site_transfer_state != 'pending_approval':
                raise UserError(_('Only transfers pending approval can be approved.'))
            pick.x_site_transfer_state = 'approved'
            pick.message_post(
                body=Markup(_('Site-to-site transfer approved by <b>%s</b>.')) % (
                    self.env.user.name))
            if pick.state not in ('done', 'cancel'):
                to_validate |= pick
        for pick in to_validate:
            res = pick.sudo().button_validate()
            if isinstance(res, dict) and res.get('type') == 'ir.actions.act_window':
                return res
        return True

    def action_reject_site_transfer(self):
        self._check_site_transfer_approver()
        for pick in self.filtered(
            lambda p: p.x_transfer_purpose == 'site_to_site' and not p.x_is_dest_receipt
        ):
            if pick.x_site_transfer_state != 'pending_approval':
                raise UserError(_('Only transfers pending approval can be rejected.'))
            pick.x_site_transfer_state = 'rejected'
            pick.message_post(
                body=Markup(_('Site-to-site transfer rejected by <b>%s</b>.')) % self.env.user.name)

    # ─────────────────────────────────────────────────────────────────────────
    # VALIDATION
    # ─────────────────────────────────────────────────────────────────────────

    def button_validate(self):
        for pick in self:
            if pick.x_transfer_purpose == 'material_issuance':
                # ── Contact is mandatory for both issuances and returns ────────
                if not pick.x_contact_id:
                    raise UserError(_(
                        'Issuance Contact is required.\n\n'
                        'Please select the employee or subcontractor '
                        'before validating.'
                    ))
                # ── Demand must not exceed available stock (issuances only) ───
                if not pick.x_is_return_transfer:
                    for move in pick.move_ids.filtered(
                        lambda m: m.product_id and m.product_uom_qty > 0
                    ):
                        avail = move.product_id.with_context(
                            location=pick.location_id.id
                        ).qty_available
                        if move.product_uom_qty > avail + 0.001:
                            raise UserError(_(
                                'Insufficient stock for "%(product)s".\n\n'
                                'Demanded: %(demand).2f %(uom)s\n'
                                'Available at %(loc)s: %(avail).2f %(uom)s\n\n'
                                'Please adjust the quantity or replenish stock first.'
                            ) % {
                                'product': move.product_id.display_name,
                                'demand': move.product_uom_qty,
                                'uom': move.product_uom.name,
                                'loc': pick.location_id.display_name,
                                'avail': avail,
                            })
            # Duplicate-asset check removed per business request — allow re-issuing
            # an asset to the same contact without requiring a return first.
            if (pick.x_transfer_purpose == 'site_to_site'
                    and not pick.x_is_dest_receipt
                    and pick.x_site_transfer_state != 'approved'):
                raise UserError(_(
                    'This site-to-site transfer must be approved by Procurement HO, CEO, '
                    'or Matracon Admin before dispatch. Use "Submit for Approval" first.'
                ))
            if pick.x_is_return_transfer:
                pick._apply_return_line_destinations()
        # Validate return quantities before posting
        for pick in self:
            if pick.x_is_return_transfer and pick.x_original_issuance_id:
                pick._check_return_quantities()
        res = super().button_validate()
        for pick in self:
            if pick.state == 'done':
                if pick.x_transfer_purpose == 'material_issuance':
                    pick._post_validate_material_issuance()
                elif pick.x_transfer_purpose == 'site_to_site':
                    if pick.x_is_dest_receipt:
                        if pick.x_source_transfer_id:
                            pick.x_source_transfer_id.write({'x_site_transfer_state': 'done'})
                    else:
                        pick._post_validate_site_to_site()
                        pick._create_destination_site_transfer()
                        pick.write({'x_site_transfer_state': 'done'})
                if pick.x_is_return_transfer:
                    pick._post_validate_return()
        return res

    def _check_duplicate_asset_issuance(self):
        """Block issuing the same asset product twice to the same contact."""
        self.ensure_one()
        if self.x_inventory_type != 'asset' or not self.x_contact_id:
            return
        for move in self.move_ids.filtered(lambda m: m.product_id):
            outstanding = self._get_outstanding_qty(
                move.product_id,
                self.x_contact_id,
                self.x_issuance_project_id,
                exclude_picking=self,
            )
            if outstanding > 0.001:
                raise UserError(_(
                    'Asset "%(product)s" is already issued to %(contact)s '
                    '(%(qty).2f still outstanding). Process a return before re-issuing.'
                ) % {
                    'product': move.product_id.display_name,
                    'contact': self.x_contact_id.name,
                    'qty': outstanding,
                })

    def _apply_return_line_destinations(self):
        """Route scrap-condition lines to the company scrap location.

        Note: In Odoo 19 the `scrap_location` Boolean field was removed from
        stock.location. We look up the standard scrap location via XML ref first,
        then fall back to a name-based search.
        """
        self.ensure_one()
        # Primary: use the standard scrap location (works in Odoo 17+)
        scrap_loc = self.env.ref('stock.stock_location_scrapped', raise_if_not_found=False)
        if not scrap_loc:
            # Fallback: find a location whose name/path contains 'scrap'
            scrap_loc = self.env['stock.location'].search([
                ('complete_name', 'ilike', 'scrap'),
                ('usage', '=', 'internal'),
                ('company_id', 'in', [False, self.company_id.id]),
            ], limit=1)
        if not scrap_loc:
            return
        for move in self.move_ids.filtered(
            lambda m: m.x_return_condition == 'scrap' and m.product_id
        ):
            move.location_dest_id = scrap_loc

    def _check_return_quantities(self):
        """Prevent returning more than was issued minus previous returns.

        Works without an original issuance reference — validates against ALL
        done issuances for this contact/project/type.
        """
        self.ensure_one()
        if not self.x_is_return_transfer:
            return
        if not self.x_contact_id or not self.x_issuance_project_id:
            return

        existing_id = (
            self._origin.id
            if hasattr(self, '_origin') and self._origin and isinstance(self._origin.id, int)
            else (self.id if isinstance(self.id, int) else False)
        )

        # ── Total issued per product for this contact/project/type ────────────
        iss_domain = [
            ('x_transfer_purpose', '=', 'material_issuance'),
            ('x_contact_id', '=', self.x_contact_id.id),
            ('x_issuance_project_id', '=', self.x_issuance_project_id.id),
            ('state', '=', 'done'),
            ('x_is_return_transfer', '=', False),
        ]
        if self.x_issue_type:
            iss_domain.append(('x_issue_type', '=', self.x_issue_type))
        if self.x_inventory_type:
            iss_domain.append(('x_inventory_type', '=', self.x_inventory_type))
        issuances = self.search(iss_domain)

        issued_by_product = {}
        for iss in issuances:
            for move in iss.move_ids.filtered(lambda m: m.state == 'done' and m.product_id):
                pid = move.product_id.id
                issued_by_product[pid] = issued_by_product.get(pid, 0.0) + move.quantity

        # ── Total already returned per product (excluding self) ───────────────
        ret_domain = [
            ('x_is_return_transfer', '=', True),
            ('x_contact_id', '=', self.x_contact_id.id),
            ('x_issuance_project_id', '=', self.x_issuance_project_id.id),
            ('state', '=', 'done'),
        ]
        if existing_id:
            ret_domain.append(('id', '!=', existing_id))
        prev_returns = self.search(ret_domain)

        returned_by_product = {}
        for ret in prev_returns:
            for move in ret.move_ids.filtered(lambda m: m.state == 'done' and m.product_id):
                pid = move.product_id.id
                returned_by_product[pid] = returned_by_product.get(pid, 0.0) + move.quantity

        # ── Validate each return line ─────────────────────────────────────────
        for move in self.move_ids.filtered(lambda m: m.product_id):
            pid = move.product_id.id
            total_issued = issued_by_product.get(pid, 0.0)
            already_returned = returned_by_product.get(pid, 0.0)
            outstanding = max(total_issued - already_returned, 0.0)
            return_qty = move.quantity or move.product_uom_qty
            if return_qty > outstanding + 0.001:
                raise UserError(_(
                    'Return quantity exceeds outstanding for "%(product)s".\n\n'
                    'Total Issued: %(issued).2f %(uom)s\n'
                    'Previously Returned: %(ret).2f %(uom)s\n'
                    'Outstanding: %(out).2f %(uom)s\n'
                    'You are trying to return: %(qty).2f %(uom)s'
                ) % {
                    'product': move.product_id.display_name,
                    'issued': total_issued,
                    'ret': already_returned,
                    'out': outstanding,
                    'qty': return_qty,
                    'uom': move.product_uom.name,
                })

    def action_load_return_lines(self):
        """Load all outstanding issued items for this contact/project/type.

        Called from the "Load Outstanding Items" button on the return form.
        No need to select the original issuance — the system finds all relevant
        issuances from the selected Contact, Issue Type, Inventory Type and Project.
        """
        self.ensure_one()
        if not self.x_contact_id:
            raise UserError(_('Please select an Issuance Contact first.'))
        if not self.x_issuance_project_id:
            raise UserError(_('Please select a Project first.'))
        if not self.x_issue_type:
            raise UserError(_('Please select an Issue Type (Normal or Subcontractor).'))
        if not self.x_inventory_type:
            raise UserError(_('Please select an Inventory Type (Asset or Consumable).'))

        # ── Find all done issuances for this contact/project/type ─────────────
        domain = [
            ('x_transfer_purpose', '=', 'material_issuance'),
            ('x_contact_id', '=', self.x_contact_id.id),
            ('x_issuance_project_id', '=', self.x_issuance_project_id.id),
            ('x_issue_type', '=', self.x_issue_type),
            ('x_inventory_type', '=', self.x_inventory_type),
            ('state', '=', 'done'),
            ('x_is_return_transfer', '=', False),
        ]
        issuances = self.search(domain, order='scheduled_date desc')
        if not issuances:
            raise UserError(_(
                'No issued materials found for %(contact)s on project %(project)s '
                '(%(type)s / %(inv)s).\n\n'
                'Make sure materials have been issued and validated first.'
            ) % {
                'contact': self.x_contact_id.name,
                'project': self.x_issuance_project_id.display_name,
                'type': dict(self.fields_get(['x_issue_type'])['x_issue_type']['selection']
                             ).get(self.x_issue_type, self.x_issue_type),
                'inv': dict(self.fields_get(['x_inventory_type'])['x_inventory_type']['selection']
                            ).get(self.x_inventory_type, self.x_inventory_type),
            })

        # ── Set picking type and locations ────────────────────────────────────
        # Use canonical site locations so old issuances with wrong dest don't
        # corrupt returns. Source = employee/sub location, dest = site stock.
        ref = issuances[0]
        issue_loc = self._get_site_issue_location()   # MCH/Employees or MCH/Subcontractor
        stock_loc = self._get_site_stock_location()    # MCH/Stock

        if issue_loc and stock_loc:
            src_loc_id = issue_loc.id
            dest_loc_id = stock_loc.id
        else:
            src_loc_id = ref.location_dest_id.id
            dest_loc_id = ref.location_id.id

        # Write picking_type_id FIRST — the base stock.picking.write() resets
        # location_id and location_dest_id to the picking type's defaults whenever
        # picking_type_id changes.  After this write, _compute_location_id fires
        # and (via our override) sets the correct site locations for a return.
        self.write({
            'x_is_return_transfer': True,
            'x_transfer_purpose': 'material_issuance',
            'picking_type_id': ref.picking_type_id.id,
        })
        # Write locations separately so no picking_type_id-triggered override runs.
        # This is belt-and-suspenders on top of our _compute_location_id override.
        self.write({
            'location_id': src_loc_id,
            'location_dest_id': dest_loc_id,
        })

        # ── Remove any existing draft lines ───────────────────────────────────
        self.move_ids.filtered(lambda m: m.state == 'draft').sudo().unlink()

        # ── Aggregate total issued qty per product ────────────────────────────
        issued_data = {}   # key=(product_id, uom_id) → {'product': ..., 'uom': ..., 'qty': ...}
        for iss in issuances:
            for move in iss.move_ids.filtered(lambda m: m.state == 'done' and m.product_id):
                key = (move.product_id.id, move.product_uom.id)
                if key not in issued_data:
                    issued_data[key] = {
                        'product': move.product_id,
                        'uom': move.product_uom,
                        'qty': 0.0,
                    }
                issued_data[key]['qty'] += move.quantity

        # ── Aggregate previously returned qty per product ─────────────────────
        existing_id = (
            self._origin.id
            if hasattr(self, '_origin') and self._origin and isinstance(self._origin.id, int)
            else (self.id if isinstance(self.id, int) else False)
        )
        prev_return_domain = [
            ('x_is_return_transfer', '=', True),
            ('x_contact_id', '=', self.x_contact_id.id),
            ('x_issuance_project_id', '=', self.x_issuance_project_id.id),
            ('x_issue_type', '=', self.x_issue_type),
            ('x_inventory_type', '=', self.x_inventory_type),
            ('state', '=', 'done'),
        ]
        if existing_id:
            prev_return_domain.append(('id', '!=', existing_id))
        prev_returns = self.search(prev_return_domain)

        returned_data = {}
        for ret in prev_returns:
            for move in ret.move_ids.filtered(lambda m: m.state == 'done' and m.product_id):
                key = (move.product_id.id, move.product_uom.id)
                returned_data[key] = returned_data.get(key, 0.0) + move.quantity

        # ── Create lines for each product with outstanding qty ────────────────
        new_moves = []
        # src_loc_id / dest_loc_id already resolved above from site config

        for key, data in issued_data.items():
            pid, uid = key
            total_issued = data['qty']
            already_returned = returned_data.get(key, 0.0)
            outstanding = total_issued - already_returned
            if outstanding > 0.001:
                new_moves.append({
                    'picking_id': self.id,
                    'product_id': pid,
                    'product_uom_qty': outstanding,
                    'quantity': outstanding,
                    'product_uom': uid,
                    'location_id': src_loc_id,
                    'location_dest_id': dest_loc_id,
                    'x_unit_cost': data['product'].standard_price,
                    # Context columns shown in the operations table
                    'x_issued_qty': total_issued,
                    'x_prev_returned_qty': already_returned,
                    'x_outstanding_return_qty': outstanding,
                })

        if not new_moves:
            raise UserError(_(
                'No outstanding items to return for %(contact)s.\n\n'
                'All previously issued quantities have already been returned.'
            ) % {'contact': self.x_contact_id.name})

        self.env['stock.move'].sudo().create(new_moves)
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'type': 'success',
                'message': _(
                    '%(count)d product(s) loaded with outstanding quantities for %(contact)s.'
                ) % {'count': len(new_moves), 'contact': self.x_contact_id.name},
                'sticky': False,
            },
        }

    def _post_validate_material_issuance(self):
        """Create a pending backcharge record for subcontractor consumable issuance.

        No journal entry or liability sheet update is made at this stage.
        Accounting is deferred to IPC submission — that is the correct point
        to recognise the liability reduction against the subcontractor.
        The IPC's "Refresh Backcharges" action will pick up all pending records
        for the same sub + project and include them in section E (Back Charges).
        """
        self.ensure_one()
        if self.x_is_return_transfer:
            return
        # Assets never generate backcharge at issuance time (handled on return)
        if self.x_inventory_type == 'asset':
            return
        if self.x_issue_type != 'subcontractor' or not self.x_backcharge_applicable:
            return
        # Guard: already created a backcharge record for this issuance
        if not self.x_contact_id or self.x_sub_backcharge_id:
            return
        if not self.x_issuance_project_id:
            return

        amount = sum(
            m.quantity * m.x_unit_cost
            for m in self.move_ids
            if m.state == 'done' and m.x_unit_cost > 0
        )
        if not amount:
            self.env['stock.picking'].flush_model(['x_backcharge_amount'])
            amount = self.x_backcharge_amount
        if not amount:
            return

        products_desc = ', '.join(
            m.product_id.display_name for m in self.move_ids if m.product_id)
        bc = self.env['x.subcontractor.backcharge'].sudo().create({
            'subcontractor_id': self.x_contact_id.id,
            'project_analytic_account_id': self.x_issuance_project_id.id,
            'description': _('Material Issuance %(ref)s — %(products)s') % {
                'ref': self.name,
                'products': products_desc[:200],
            },
            'amount': amount,
            'date': fields.Date.context_today(self),
        })
        self.x_sub_backcharge_id = bc
        self.message_post(
            body=Markup(_(
                'Subcontractor Backcharge <b>%s</b> (%.2f) created — '
                'pending IPC processing. Liability sheet will be updated when '
                'an IPC is submitted for this subcontractor.'
            )) % (bc.name, amount)
        )

    def _post_validate_return(self):
        """Reverse backcharge on subcontractor returns; post damage charges if needed."""
        self.ensure_one()
        orig = self.x_original_issuance_id
        if not orig:
            return

        # ── Consumable return: adjust or cancel the pending backcharge ─────────
        # NEW FLOW (issuance used new backcharge-record approach):
        #   reduce or delete the pending x.subcontractor.backcharge so it is no
        #   longer picked up by "Refresh Backcharges" on IPC creation.
        # LEGACY FLOW (issuance already created a journal entry — old behaviour):
        #   reverse the journal entry so accounting stays consistent.
        if (orig.x_issue_type == 'subcontractor'
                and orig.x_backcharge_applicable
                and orig.x_inventory_type != 'asset'):
            pending_bc = orig.x_sub_backcharge_id

            if pending_bc and pending_bc.state == 'pending':
                # ── New flow: adjust the pending backcharge record ────────────
                orig_qty = sum(
                    m.quantity for m in orig.move_ids if m.state == 'done') or 1.0
                ret_qty = sum(m.quantity for m in self.move_ids if m.state == 'done')
                proportion = min(ret_qty / orig_qty, 1.0)
                adj_amount = round(pending_bc.amount * proportion, 2)
                new_amount = round(max(pending_bc.amount - adj_amount, 0.0), 2)
                if new_amount <= 0.01:
                    bc_name = pending_bc.name
                    pending_bc.sudo().unlink()
                    orig.x_sub_backcharge_id = False
                    self.message_post(
                        body=Markup(_(
                            'Pending backcharge <b>%s</b> cancelled — '
                            'all issued materials have been returned.'
                        )) % bc_name
                    )
                else:
                    pending_bc.sudo().write({'amount': new_amount})
                    self.message_post(
                        body=Markup(_(
                            'Pending backcharge <b>%s</b> reduced by %.2f '
                            'to %.2f — partial return (%.0f%% returned).'
                        )) % (pending_bc.name, adj_amount, new_amount,
                               proportion * 100)
                    )

            elif orig.x_backcharge_refund_entry_id and not self.x_return_backcharge_entry_id:
                # ── Legacy flow: original issuance created a journal entry ─────
                # Reverse it so accounting stays balanced with old records.
                orig_amount = sum(
                    m.quantity * m.x_unit_cost
                    for m in orig.move_ids
                    if m.state == 'done' and m.x_unit_cost > 0
                ) or orig.x_backcharge_amount
                orig_qty = sum(
                    m.quantity for m in orig.move_ids if m.state == 'done') or 1.0
                ret_qty = sum(m.quantity for m in self.move_ids if m.state == 'done')
                proportion = ret_qty / orig_qty
                adj_amount = round(orig_amount * proportion, 2)
                if adj_amount > 0:
                    entry = self._create_issuance_journal_entry(
                        adj_amount, is_return=True, original=orig)
                    if entry:
                        self.x_return_backcharge_entry_id = entry
                        self.message_post(
                            body=Markup(_(
                                'Vendor Bill <b>%s</b> (%.2f) created — '
                                'returned materials added back to subcontractor '
                                'payable (legacy flow).'
                            )) % (entry.name, adj_amount)
                        )
                        self._auto_adjust_liability_sheet_on_return(
                            adj_amount, orig)

        # ── Asset return: manual backcharge amount entered by user ────────────
        if (orig.x_inventory_type == 'asset'
                and self.x_return_backcharge_applicable
                and self.x_return_backcharge_amount > 0
                and not self.x_return_backcharge_entry_id):
            entry = self._create_damage_journal_entry(
                self.x_return_backcharge_amount, orig)
            if entry:
                self.x_return_backcharge_entry_id = entry
                self.message_post(
                    body=Markup(_(
                        'Vendor Credit Note <b>%s</b> (%.2f) created — '
                        'asset return backcharge deducted from subcontractor payable.'
                    )) % (entry.name, self.x_return_backcharge_amount)
                )
                self._auto_update_liability_sheet(self.x_return_backcharge_amount)

        # Damage backcharge only for consumables — assets use the picking-level
        # x_return_backcharge_applicable / x_return_backcharge_amount flow above.
        if (self.x_return_type == 'damaged'
                and not self.x_damage_backcharge_entry_id
                and orig.x_inventory_type != 'asset'):
            self._post_validate_damage_backcharge(orig)

        # ── Create x.employee.backcharge for normal-issue returns with damage ─
        if (orig.x_issue_type == 'normal'
                and self.x_contact_id
                and not self.x_employee_backcharge_id):
            damage_amount = 0.0
            if self.x_return_type == 'damaged':
                damage_amount = sum(self.move_ids.mapped('x_damage_amount'))
                if not damage_amount and orig.x_inventory_type == 'asset':
                    damage_amount = sum(
                        m.quantity * m.product_id.standard_price
                        for m in self.move_ids if m.state == 'done')
            elif (orig.x_inventory_type == 'asset'
                    and self.x_return_backcharge_applicable
                    and self.x_return_backcharge_amount):
                damage_amount = self.x_return_backcharge_amount
            if damage_amount > 0:
                employee = self.env['hr.employee'].search([
                    '|',
                    ('work_contact_id', '=', self.x_contact_id.id),
                    ('address_id', '=', self.x_contact_id.id),
                ], limit=1)
                if not employee:
                    employee = self.env['hr.employee'].search([
                        ('name', 'ilike', self.x_contact_id.name),
                    ], limit=1)
                if employee:
                    products_desc = ', '.join(
                        m.product_id.display_name
                        for m in self.move_ids if m.product_id
                    )
                    bc = self.env['x.employee.backcharge'].sudo().create({
                        'employee_id': employee.id,
                        'project_analytic_account_id': (
                            self.x_issuance_project_id.id
                            if self.x_issuance_project_id else False),
                        'description': _('Return from %(ref)s — %(products)s') % {
                            'ref': self.name,
                            'products': products_desc[:200],
                        },
                        'backcharge_amount': damage_amount,
                        'backcharge_date': fields.Date.context_today(self),
                        'issue_date': (orig.scheduled_date.date()
                                       if orig.scheduled_date else False),
                        'notes': self.x_return_remarks or '',
                    })
                    self.x_employee_backcharge_id = bc
                    self.message_post(
                        body=Markup(_(
                            'Employee Backcharge <b>%s</b> (%.2f) created for %s.'
                        )) % (bc.name, damage_amount, employee.name)
                    )

        # ── Create x.subcontractor.backcharge for sub returns with damage ─────
        if (orig.x_issue_type == 'subcontractor'
                and self.x_contact_id
                and not self.x_sub_backcharge_id):
            damage_amount = 0.0
            if self.x_return_type == 'damaged':
                damage_amount = sum(self.move_ids.mapped('x_damage_amount'))
                if not damage_amount and orig.x_inventory_type == 'asset':
                    damage_amount = sum(
                        m.quantity * m.product_id.standard_price
                        for m in self.move_ids if m.state == 'done')
            elif (orig.x_inventory_type == 'asset'
                    and self.x_return_backcharge_applicable
                    and self.x_return_backcharge_amount):
                damage_amount = self.x_return_backcharge_amount
            if damage_amount > 0 and self.x_issuance_project_id:
                products_desc = ', '.join(
                    m.product_id.display_name for m in self.move_ids if m.product_id)
                bc = self.env['x.subcontractor.backcharge'].sudo().create({
                    'subcontractor_id': self.x_contact_id.id,
                    'project_analytic_account_id': self.x_issuance_project_id.id,
                    'description': _('Material Return %(ref)s — %(products)s') % {
                        'ref': self.name,
                        'products': products_desc[:200],
                    },
                    'amount': damage_amount,
                    'date': fields.Date.context_today(self),
                })
                self.x_sub_backcharge_id = bc
                self.message_post(
                    body=Markup(_(
                        'Subcontractor Backcharge <b>%s</b> (%.2f) created.'
                    )) % (bc.name, damage_amount)
                )

    def _post_validate_damage_backcharge(self, original):
        """Separate damage backcharge for incomplete / damaged asset returns."""
        self.ensure_one()
        amount = sum(self.move_ids.mapped('x_damage_amount'))
        if amount <= 0 and original.x_issue_type == 'subcontractor':
            amount = sum(
                m.quantity * m.x_unit_cost
                for m in self.move_ids if m.state == 'done' and m.x_unit_cost > 0
            )
        if amount <= 0 or not self.x_contact_id:
            return
        entry = self._create_damage_journal_entry(amount, original)
        if entry:
            self.x_damage_backcharge_entry_id = entry
            self.message_post(
                body=Markup(_(
                    'Vendor Credit Note <b>%s</b> (%.2f) created — '
                    'damage backcharge deducted from subcontractor payable.'
                )) % (entry.name, amount)
            )
            self._auto_update_liability_sheet(amount)

    def _post_validate_site_to_site(self):
        """Inter-project receivable (source) and payable (destination) entry."""
        self.ensure_one()
        if self.x_interproject_entry_id or self.x_is_dest_receipt:
            return
        src_project = self.x_issuance_project_id
        dst_project = self.x_dest_project_id
        if not src_project or not dst_project:
            return
        receivable_account = self._get_or_create_interproject_account('receivable')
        payable_account = self._get_or_create_interproject_account('payable')
        journal = self._get_or_create_interproject_journal()
        total_value = sum(
            m.quantity * (m.product_id.standard_price or 0.0)
            for m in self.move_ids if m.state == 'done'
        )
        if total_value <= 0:
            return
        # Source is owed (receivable); destination owes source (payable)
        aml_vals = [
            {
                'account_id': receivable_account.id,
                'name': _('Inter-project receivable: %s from %s') % (
                    src_project.name, dst_project.name),
                'debit': total_value,
                'credit': 0.0,
                'analytic_distribution': {str(src_project.id): 100},
            },
            {
                'account_id': payable_account.id,
                'name': _('Inter-project payable: %s to %s') % (
                    dst_project.name, src_project.name),
                'debit': 0.0,
                'credit': total_value,
                'analytic_distribution': {str(dst_project.id): 100},
            },
        ]
        move = self.env['account.move'].sudo().create({
            'move_type': 'entry',
            'journal_id': journal.id,
            'ref': _('Site-to-Site %s: %s → %s') % (
                self.name, src_project.name, dst_project.name),
            'line_ids': [(0, 0, v) for v in aml_vals],
        })
        move.action_post()
        self.x_interproject_entry_id = move

        # Register in the inter-project transfer ledger so FO can track
        # material-based balances alongside cash-based ones.
        self.env['x.interproject.transfer'].sudo().create({
            'date': fields.Date.context_today(self),
            'transfer_type': 'inventory',
            'picking_id': self.id,
            'move_id': move.id,
            'source_analytic_id': src_project.id,
            'dest_analytic_id': dst_project.id,
            'amount': total_value,
        })

        self.message_post(
            body=Markup(_(
                'Inter-project entry <b>%s</b> created — Receivable on <b>%s</b>, '
                'Payable on <b>%s</b> (%.2f).'
            )) % (move.name, src_project.name, dst_project.name, total_value)
        )

    def _create_destination_site_transfer(self):
        """Create incoming transfer at destination site for acknowledgement."""
        self.ensure_one()
        if self.x_dest_picking_id or self.x_is_dest_receipt:
            return
        dest_config = self._get_site_config_for_analytic(self.x_dest_project_id)
        dest_wh = dest_config.warehouse_id
        if not dest_wh:
            raise UserError(_(
                'Destination project "%s" has no warehouse in Site Project Configuration.'
            ) % self.x_dest_project_id.name)

        move_vals = []
        for move in self.move_ids.filtered(lambda m: m.state == 'done' and m.product_id):
            move_vals.append((0, 0, {
                'product_id': move.product_id.id,
                'product_uom': move.product_uom.id,
                'product_uom_qty': move.quantity,
                'location_id': self.location_dest_id.id,
                'location_dest_id': dest_wh.lot_stock_id.id,
            }))
        if not move_vals:
            return

        dest_picking = self.create({
            'picking_type_id': dest_wh.int_type_id.id,
            'location_id': self.location_dest_id.id,
            'location_dest_id': dest_wh.lot_stock_id.id,
            'x_transfer_purpose': 'site_to_site',
            'x_is_dest_receipt': True,
            'x_source_transfer_id': self.id,
            'x_issuance_project_id': self.x_dest_project_id.id,
            'x_dest_project_id': self.x_issuance_project_id.id,
            'x_site_transfer_state': 'approved',
            'origin': self.name,
            'move_ids': move_vals,
        })
        dest_picking.action_confirm()
        dest_picking.action_assign()
        self.x_dest_picking_id = dest_picking.id
        self.message_post(
            body=Markup(_(
                'Destination receipt <b>%s</b> created for <b>%s</b>. '
                'Destination site store must validate receipt.'
            )) % (dest_picking.name, self.x_dest_project_id.name)
        )
        dest_picking.message_post(
            body=Markup(_('Incoming site-to-site transfer from <b>%s</b> (%s).')) % (
                self.x_issuance_project_id.name, self.name)
        )
        # Notify destination site store users so they know materials are on the way
        if dest_config and dest_config.site_user_ids:
            matracon_notify.notify_users(
                dest_picking,
                dest_config.site_user_ids,
                _('Site-to-site transfer <b>%s</b> is on the way from <b>%s</b>. '
                  'Please validate receipt when materials arrive.')
                % (self.name, self.x_issuance_project_id.name),
                summary=_('Incoming Site Transfer'),
            )

    # ─────────────────────────────────────────────────────────────────────────
    # ACCOUNTING HELPERS
    # ─────────────────────────────────────────────────────────────────────────

    def _create_issuance_journal_entry(self, amount, is_return=False, original=None):
        """Create a proper vendor accounting document for backcharge / return.

        Backcharge on issuance → Vendor Credit Note (in_refund)
          We issued material to the subcontractor → we deduct from what we owe them.
          e.g. Contract $100k, backcharge $50k → we owe $50k.

        Return of materials → Vendor Bill (in_invoice)
          Subcontractor returned material → we owe them more again.
          e.g. They return $25k worth → we owe $75k.
        """
        self.ensure_one()
        journal = self._get_or_create_backcharge_journal()
        partner = self.x_contact_id
        analytic_id = self.x_issuance_project_id.id if self.x_issuance_project_id else False

        if not partner:
            self.message_post(body=_(
                'Warning: no subcontractor set on this transfer — '
                'backcharge document skipped.'))
            return None

        Account = self.env['account.account'].sudo()
        expense_account = Account.search(
            [('account_type', 'in', ['expense', 'expense_direct_cost'])], limit=1)

        if not expense_account:
            self.message_post(body=_(
                'Warning: expense account not found. '
                'Configure Chart of Accounts and retry.'))
            return None

        label = (self.x_backcharge_description
                 or (_('Material Return: %s') % (original.name if original else self.name)
                     if is_return
                     else _('Backcharge: %s') % self.name))
        analytic = {str(analytic_id): 100} if analytic_id else {}

        # Backcharge on issuance → Vendor Credit Note (reduces AP / what we owe)
        # Return of materials   → Vendor Bill       (increases AP / what we owe)
        move_type = 'in_invoice' if is_return else 'in_refund'

        move = self.env['account.move'].sudo().create({
            'move_type': move_type,
            'journal_id': journal.id,
            'partner_id': partner.id,
            'ref': label,
            'invoice_date': fields.Date.today(),
            'narration': self.x_backcharge_description or False,
            'x_source_picking_id': self.id,
            'invoice_line_ids': [(0, 0, {
                'name': label,
                'quantity': 1.0,
                'price_unit': amount,
                'account_id': expense_account.id,
                'analytic_distribution': analytic,
            })],
        })
        move.action_post()
        return move

    def _create_damage_journal_entry(self, amount, original):
        """Damage backcharge → Vendor Credit Note (reduces what we owe the subcontractor)."""
        self.ensure_one()
        journal = self._get_or_create_backcharge_journal()
        partner = self.x_contact_id or original.x_contact_id
        analytic_id = original.x_issuance_project_id.id if original.x_issuance_project_id else False

        if not partner:
            self.message_post(body=_('Warning: no partner — damage entry skipped.'))
            return None

        Account = self.env['account.account'].sudo()
        expense_account = Account.search(
            [('account_type', 'in', ['expense', 'expense_direct_cost'])], limit=1)

        if not expense_account:
            self.message_post(body=_(
                'Warning: expense account not found — damage entry skipped.'))
            return None

        label = _('Damage Backcharge — Return %s') % self.name
        analytic = {str(analytic_id): 100} if analytic_id else {}

        move = self.env['account.move'].sudo().create({
            'move_type': 'in_refund',
            'journal_id': journal.id,
            'partner_id': partner.id,
            'ref': label,
            'invoice_date': fields.Date.today(),
            'x_source_picking_id': self.id,
            'invoice_line_ids': [(0, 0, {
                'name': label,
                'quantity': 1.0,
                'price_unit': amount,
                'account_id': expense_account.id,
                'analytic_distribution': analytic,
            })],
        })
        move.action_post()
        return move

    def _auto_update_liability_sheet(self, amount):
        """Auto-create or update the current-month liability sheet for this project.

        `amount` is passed in directly (computed from done moves) rather than
        reading the stored x_backcharge_amount which may be stale post-validation.
        """
        self.ensure_one()
        if not self.x_issuance_project_id or not self.x_contact_id or not amount:
            return
        today = fields.Date.today()
        month_start = today.replace(day=1)
        month_end = (month_start + relativedelta(months=1)) - relativedelta(days=1)

        LiabilitySheet = self.env['x.liability.sheet'].sudo()
        sheet = LiabilitySheet.search([
            ('project_analytic_account_id', '=', self.x_issuance_project_id.id),
            ('date_from', '=', month_start),
            ('state', '=', 'draft'),
        ], limit=1)

        if not sheet:
            sheet = LiabilitySheet.create({
                'project_analytic_account_id': self.x_issuance_project_id.id,
                'date_from': month_start,
                'date_to': month_end,
            })
            self.message_post(
                body=Markup(_('Liability Sheet <b>%s</b> auto-created for %s.')) % (
                    sheet.name, self.x_issuance_project_id.name)
            )

        # Accumulate on existing line for same partner — one line per vendor
        existing_line = sheet.line_ids.filtered(
            lambda l: l.partner_id.id == self.x_contact_id.id
        )
        if existing_line:
            line = existing_line[0]
            line.write({
                'new_liability': line.new_liability + amount,
                'recommended_amount': line.recommended_amount + amount,
            })
            self.message_post(
                body=Markup(_('Liability Sheet <b>%s</b> updated for <b>%s</b>: +%s (total %s)')) % (
                    sheet.name,
                    self.x_contact_id.name,
                    f'{amount:,.2f}',
                    f'{line.new_liability:,.2f}',
                )
            )
        else:
            # Description always from the contact's x_description — never from
            # the picking name — so the label stays stable across all entries.
            partner_desc = (
                self.x_contact_id.x_description or self.x_contact_id.name or ''
            ).strip()
            sheet.write({
                'line_ids': [(0, 0, {
                    'description': partner_desc,
                    'partner_id': self.x_contact_id.id,
                    'new_liability': amount,
                    'recommended_amount': amount,
                })]
            })
            self.message_post(
                body=Markup(_('Line added to Liability Sheet <b>%s</b>: %s — %s')) % (
                    sheet.name, partner_desc, f'{amount:,.2f}')
            )

    def _auto_adjust_liability_sheet_on_return(self, adj_amount, original):
        """Reduce the liability sheet line for the original issuance when items are returned."""
        self.ensure_one()
        if not original.x_issuance_project_id:
            return
        LiabilitySheet = self.env['x.liability.sheet'].sudo()
        # Look in draft or submitted sheets (not yet approved/paid)
        sheets = LiabilitySheet.search([
            ('project_analytic_account_id', '=', original.x_issuance_project_id.id),
            ('state', 'in', ['draft', 'submitted']),
        ])
        for sheet in sheets:
            for line in sheet.line_ids:
                if line.partner_id == original.x_contact_id:
                    new_liability = max(line.new_liability - adj_amount, 0.0)
                    new_recommended = max(line.recommended_amount - adj_amount, 0.0)
                    line.write({
                        'new_liability': new_liability,
                        'recommended_amount': new_recommended,
                    })
                    self.message_post(
                        body=Markup(_(
                            'Liability Sheet <b>%s</b> updated for <b>%s</b>: '
                            'reduced by <b>%s</b>.'
                        )) % (
                            sheet.name,
                            original.x_contact_id.name,
                            f'{adj_amount:,.2f}',
                        )
                    )
                    return
        # No matching line found — add a note so it can be handled manually
        self.message_post(
            body=_(
                'Return adjustment of %s could not be automatically applied to a '
                'Liability Sheet (no matching draft line found for %s).'
            ) % (f'{adj_amount:,.2f}', original.name)
        )

    def _get_or_create_backcharge_journal(self):
        # Use sudo() — journal is a system resource, not a user resource.
        # Site store users are allowed to validate pickings; the system
        # manages the accounting infrastructure on their behalf.
        Journal = self.env['account.journal'].sudo()
        journal = Journal.search(
            [('name', '=', 'Backcharge'), ('type', '=', 'purchase')], limit=1)
        if not journal:
            journal = Journal.create({
                'name': 'Backcharge',
                'type': 'purchase',
                'code': 'BCHRG',
            })
        return journal

    def _get_or_create_backcharge_account(self):
        Account = self.env['account.account'].sudo()
        account = Account.search(
            [('name', 'ilike', 'Backcharge'),
             ('account_type', '=', 'liability_payable')], limit=1)
        if not account:
            account = Account.search(
                [('account_type', '=', 'liability_payable')], limit=1)
        return account

    def _get_or_create_interproject_journal(self):
        Journal = self.env['account.journal'].sudo()
        journal = Journal.search(
            [('name', '=', 'Inter-Project Transfers'),
             ('type', '=', 'general')], limit=1)
        if not journal:
            journal = Journal.create({
                'name': 'Inter-Project Transfers',
                'type': 'general',
                'code': 'IPTR',
            })
        return journal

    def _get_or_create_interproject_account(self, account_type):
        Account = self.env['account.account'].sudo()
        if account_type == 'receivable':
            account = Account.search(
                [('name', 'ilike', 'Inter-Project Receivable')], limit=1)
            if not account:
                account = Account.create({
                    'name': 'Inter-Project Receivables',
                    'code': '13100',
                    'account_type': 'asset_receivable',
                    'reconcile': True,
                })
        else:
            account = Account.search(
                [('name', 'ilike', 'Inter-Project Payable')], limit=1)
            if not account:
                account = Account.create({
                    'name': 'Inter-Project Payables',
                    'code': '21100',
                    'account_type': 'liability_payable',
                    'reconcile': True,
                })
        return account

    # ─────────────────────────────────────────────────────────────────────────
    # ACTIONS
    # ─────────────────────────────────────────────────────────────────────────

    def action_view_dest_receipt(self):
        self.ensure_one()
        return {
            'name': _('Destination Receipt'),
            'type': 'ir.actions.act_window',
            'res_model': 'stock.picking',
            'view_mode': 'form',
            'res_id': self.x_dest_picking_id.id,
        }

    def action_view_source_transfer(self):
        self.ensure_one()
        return {
            'name': _('Source Transfer'),
            'type': 'ir.actions.act_window',
            'res_model': 'stock.picking',
            'view_mode': 'form',
            'res_id': self.x_source_transfer_id.id,
        }

    def action_return_material(self):
        """Open a new stock.picking form pre-filled as a return from this issuance."""
        self.ensure_one()
        return {
            'name': _('Return Material'),
            'type': 'ir.actions.act_window',
            'res_model': 'stock.picking',
            'view_mode': 'form',
            'context': {
                'default_x_transfer_purpose': 'material_issuance',
                'default_x_is_return_transfer': True,
                'default_x_original_issuance_id': self.id,
                'default_x_contact_id': self.x_contact_id.id,
                'default_x_issuance_project_id': self.x_issuance_project_id.id,
                'default_x_inventory_type': self.x_inventory_type,
                'default_picking_type_id': self.picking_type_id.id,
                'default_location_id': self.location_dest_id.id,
                'default_location_dest_id': self.location_id.id,
                'default_origin': self.name,
            },
        }

    def action_view_returns(self):
        """Open list of return transfers linked to this issuance."""
        self.ensure_one()
        return {
            'name': _('Returns for %s') % self.name,
            'type': 'ir.actions.act_window',
            'res_model': 'stock.picking',
            'view_mode': 'list,form',
            'domain': [('x_original_issuance_id', '=', self.id)],
            'context': {
                'default_x_is_return_transfer': True,
                'default_x_transfer_purpose': 'material_issuance',
                'default_x_original_issuance_id': self.id,
            },
        }

    def action_view_employee_backcharge(self):
        """Open the employee backcharge linked to this return."""
        self.ensure_one()
        if not self.x_employee_backcharge_id:
            return False
        return {
            'type': 'ir.actions.act_window',
            'name': _('Employee Backcharge'),
            'res_model': 'x.employee.backcharge',
            'view_mode': 'form',
            'res_id': self.x_employee_backcharge_id.id,
        }

    def action_view_sub_backcharge(self):
        """Open the subcontractor backcharge linked to this return."""
        self.ensure_one()
        if not self.x_sub_backcharge_id:
            return False
        return {
            'type': 'ir.actions.act_window',
            'name': _('Subcontractor Backcharge'),
            'res_model': 'x.subcontractor.backcharge',
            'view_mode': 'form',
            'res_id': self.x_sub_backcharge_id.id,
        }

    def action_view_backcharge_entries(self):
        """Open backcharge journal entries linked to this issuance."""
        self.ensure_one()
        Move = self.env['account.move'].sudo()
        entry_ids = []
        for field_name in (
            'x_backcharge_refund_entry_id',
            'x_return_backcharge_entry_id',
            'x_damage_backcharge_entry_id',
        ):
            entry = getattr(self, field_name)
            if entry:
                entry_ids.append(entry.id)
        if not entry_ids:
            entry_ids = Move.search([
                ('x_source_picking_id', '=', self.id),
                ('move_type', 'in', ['entry', 'in_invoice', 'in_refund']),
            ]).ids
        if not entry_ids:
            raise UserError(_('No backcharge journal entry found for this transfer.'))
        if len(entry_ids) == 1:
            return {
                'name': _('Backcharge Entry'),
                'type': 'ir.actions.act_window',
                'res_model': 'account.move',
                'view_mode': 'form',
                'res_id': entry_ids[0],
                'views': [(False, 'form')],
                'context': {'create': False},
            }
        list_view = self.env.ref(
            'site_operations.view_backcharge_move_list', raise_if_not_found=False)
        return {
            'name': _('Backcharge Entries'),
            'type': 'ir.actions.act_window',
            'res_model': 'account.move',
            'view_mode': 'list,form',
            'domain': [('id', 'in', entry_ids)],
            'views': [
                (list_view.id if list_view else False, 'list'),
                (False, 'form'),
            ],
            'context': {'create': False},
        }

    def action_view_interproject_entry(self):
        """Open inter-project accounting entry."""
        self.ensure_one()
        return {
            'name': _('Inter-Project Entry'),
            'type': 'ir.actions.act_window',
            'res_model': 'account.move',
            'view_mode': 'form',
            'res_id': self.x_interproject_entry_id.id,
        }

    def action_print_mif(self):
        return self.env.ref('site_operations.action_report_mif').report_action(self)

    def action_print_mtn(self):
        return self.env.ref('site_operations.action_report_mtn').report_action(self)

    def action_print_gate_pass(self):
        return self.env.ref(
            'site_operations.action_report_gate_pass').report_action(self)

    x_vendor_bill_count = fields.Integer(compute='_compute_x_vendor_bill_count')

    @api.depends('purchase_id')
    def _compute_x_vendor_bill_count(self):
        Bill = self.env['account.move']
        for picking in self:
            if picking.purchase_id:
                picking.x_vendor_bill_count = Bill.search_count([
                    ('move_type', '=', 'in_invoice'),
                    ('x_purchase_order_id', '=', picking.purchase_id.id),
                ])
            else:
                picking.x_vendor_bill_count = 0

    def action_view_vendor_bills(self):
        self.ensure_one()
        bills = self.env['account.move']
        if self.purchase_id:
            bills = bills.search([
                ('move_type', '=', 'in_invoice'),
                ('x_purchase_order_id', '=', self.purchase_id.id),
            ])
        return {
            'type': 'ir.actions.act_window',
            'name': _('Vendor Bills'),
            'res_model': 'account.move',
            'view_mode': 'list,form',
            'domain': [('id', 'in', bills.ids)],
        }
