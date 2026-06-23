from datetime import date as dt_date
from dateutil.relativedelta import relativedelta

from odoo import models, fields, api, _
from odoo.exceptions import UserError


class StockPickingSiteOps(models.Model):
    _inherit = 'stock.picking'

    # ── Transfer classification ───────────────────────────────────────────────
    x_transfer_purpose = fields.Selection([
        ('material_issuance', 'Material Issuance'),
        ('site_to_site', 'Site To Site Transfer'),
    ], string='Transfer Purpose', default='material_issuance', tracking=True)

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
        'stock.picking', string='Original Issuance', readonly=True)
    x_return_type = fields.Selection([
        ('normal', 'Normal Return'),
        ('damaged', 'Damaged / Lost'),
    ], string='Return Type')
    x_return_condition = fields.Char(string='Return Condition')
    x_return_remarks = fields.Text(string='Return Remarks')
    x_return_backcharge_applicable = fields.Boolean(string='Backcharge on Return')
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

    # ── Smart button counts ───────────────────────────────────────────────────
    x_return_count = fields.Integer(
        string='Return Transfers', compute='_compute_x_return_count', store=False)
    x_backcharge_entry_count = fields.Integer(
        string='Backcharge Entries', compute='_compute_entry_counts', store=False)
    x_interproject_entry_count = fields.Integer(
        string='Inter-Project Entries', compute='_compute_entry_counts', store=False)

    # ─────────────────────────────────────────────────────────────────────────
    # COMPUTE
    # ─────────────────────────────────────────────────────────────────────────

    def _compute_x_is_site_store(self):
        is_store = self.env.user.has_group('purchase_demand_raise.group_site_store')
        for pick in self:
            pick.x_is_site_store = is_store

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
                pt = self.env['stock.picking.type'].search(
                    [('code', '=', 'internal')], limit=1)
            if pt and not res.get('picking_type_id'):
                res['picking_type_id'] = pt.id
                if pt.default_location_src_id:
                    res.setdefault('location_id', pt.default_location_src_id.id)
                if pt.default_location_dest_id:
                    res.setdefault('location_dest_id', pt.default_location_dest_id.id)
            if res.get('x_transfer_purpose') == 'material_issuance' and not res.get('location_dest_id'):
                customer_loc = self.env['stock.location'].search([
                    ('usage', '=', 'customer'),
                    ('company_id', 'in', [False, self.env.company.id]),
                ], limit=1)
                if customer_loc:
                    res['location_dest_id'] = customer_loc.id
            res.setdefault('x_generate_gate_pass', True)
            if res.get('x_issue_type') == 'subcontractor':
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

    @api.onchange('x_issue_type')
    def _onchange_issue_type_backcharge(self):
        if self.x_transfer_purpose != 'material_issuance':
            return
        if self.x_issue_type == 'subcontractor':
            self.x_backcharge_applicable = True
        elif self.x_issue_type == 'normal':
            self.x_backcharge_applicable = False

    @api.onchange('x_contact_id', 'x_issuance_project_id', 'move_ids', 'move_ids.product_id')
    def _onchange_contact_outstanding_preview(self):
        """Refresh outstanding materials summary live in the form."""
        self._compute_outstanding_materials()

    @api.onchange('picking_type_id', 'x_transfer_purpose')
    def _onchange_site_ops_picking_type(self):
        """Ensure source/dest locations are set for material issuance forms."""
        if self.x_transfer_purpose not in ('material_issuance', 'site_to_site'):
            return
        if self.picking_type_id:
            if self.picking_type_id.default_location_src_id:
                self.location_id = self.picking_type_id.default_location_src_id
            if (self.picking_type_id.default_location_dest_id
                    and self.x_transfer_purpose == 'material_issuance'):
                self.location_dest_id = self.picking_type_id.default_location_dest_id
        if not self.scheduled_date:
            self.scheduled_date = fields.Datetime.now()

    @api.onchange('x_contact_id', 'x_issue_type')
    def _onchange_contact_destination(self):
        """Set delivery destination for material issuance."""
        if self.x_transfer_purpose != 'material_issuance':
            return
        if self.x_contact_id and self.x_contact_id.property_stock_customer:
            self.location_dest_id = self.x_contact_id.property_stock_customer
        elif not self.location_dest_id:
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
        return self.env['x.project.site.config'].search(
            [('analytic_account_id', '=', analytic_account.id)], limit=1)

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
                        pt = self.env['stock.picking.type'].search(
                            [('code', '=', 'internal')], limit=1)
                    if pt:
                        vals['picking_type_id'] = pt.id
                        vals.setdefault('location_id',
                                        pt.default_location_src_id.id if pt.default_location_src_id else False)
                        vals.setdefault('location_dest_id',
                                        pt.default_location_dest_id.id if pt.default_location_dest_id else False)
                if vals.get('x_transfer_purpose') == 'material_issuance' and not vals.get('location_dest_id'):
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
                pick.x_outstanding_materials_html = (
                    header + ''.join(lines) + '</tbody></table>'
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
                all_returns = self.search([
                    ('x_original_issuance_id', '=', orig.id),
                    ('state', '=', 'done'),
                ])
                pick.x_total_returned_qty = sum(
                    all_returns.mapped('move_ids').mapped('quantity'))
                pick.x_outstanding_qty = (
                    pick.x_original_issued_qty - pick.x_total_returned_qty)
            elif not pick.x_is_return_transfer:
                pick.x_original_issued_qty = sum(pick.move_ids.mapped('quantity'))
                all_returns = self.search([
                    ('x_original_issuance_id', '=', pick.id),
                    ('state', '=', 'done'),
                ])
                pick.x_total_returned_qty = sum(
                    all_returns.mapped('move_ids').mapped('quantity'))
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

    # ─────────────────────────────────────────────────────────────────────────
    # SITE-TO-SITE APPROVAL
    # ─────────────────────────────────────────────────────────────────────────

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
                body=_('Material Transfer Note submitted for approval by <b>%s</b>.') % (
                    self.env.user.name))

    def action_approve_site_transfer(self):
        for pick in self.filtered(
            lambda p: p.x_transfer_purpose == 'site_to_site' and not p.x_is_dest_receipt
        ):
            if pick.x_site_transfer_state != 'pending_approval':
                raise UserError(_('Only transfers pending approval can be approved.'))
            pick.x_site_transfer_state = 'approved'
            pick.message_post(
                body=_('Site-to-site transfer approved by <b>%s</b>.') % self.env.user.name)

    def action_reject_site_transfer(self):
        for pick in self.filtered(
            lambda p: p.x_transfer_purpose == 'site_to_site' and not p.x_is_dest_receipt
        ):
            if pick.x_site_transfer_state != 'pending_approval':
                raise UserError(_('Only transfers pending approval can be rejected.'))
            pick.x_site_transfer_state = 'rejected'
            pick.message_post(
                body=_('Site-to-site transfer rejected by <b>%s</b>.') % self.env.user.name)

    # ─────────────────────────────────────────────────────────────────────────
    # VALIDATION
    # ─────────────────────────────────────────────────────────────────────────

    def button_validate(self):
        for pick in self:
            if pick.x_transfer_purpose == 'material_issuance' and not pick.x_is_return_transfer:
                pick._check_duplicate_asset_issuance()
            if (pick.x_transfer_purpose == 'site_to_site'
                    and not pick.x_is_dest_receipt
                    and pick.x_site_transfer_state != 'approved'):
                raise UserError(_(
                    'This site-to-site transfer must be approved by Procurement HO or CEO '
                    'before dispatch. Use "Submit for Approval" first.'
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
        """Route scrap-condition lines to the company scrap location."""
        self.ensure_one()
        scrap_loc = self.env['stock.location'].search([
            ('scrap_location', '=', True),
            ('company_id', '=', self.company_id.id),
        ], limit=1)
        if not scrap_loc:
            return
        for move in self.move_ids.filtered(
            lambda m: m.x_return_condition == 'scrap' and m.product_id
        ):
            move.location_dest_id = scrap_loc

    def _check_return_quantities(self):
        """Prevent returning more than was originally issued (minus previous returns)."""
        self.ensure_one()
        orig = self.x_original_issuance_id
        if not orig:
            return

        # Previous done returns for the same original (excluding self)
        prev_returns = self.search([
            ('x_original_issuance_id', '=', orig.id),
            ('state', '=', 'done'),
            ('id', '!=', self._origin.id if self._origin else self.id),
        ])

        for move in self.move_ids:
            issued = sum(
                m.quantity for m in orig.move_ids
                if m.product_id == move.product_id and m.state == 'done'
            )
            already_returned = sum(
                m.quantity for ret in prev_returns
                for m in ret.move_ids
                if m.product_id == move.product_id
            )
            outstanding = issued - already_returned
            if move.quantity > outstanding + 0.001:
                raise UserError(_(
                    'Cannot return %(qty).2f %(uom)s of "%(product)s".\n'
                    'Originally issued: %(issued).2f — Already returned: %(ret).2f — '
                    'Outstanding: %(out).2f'
                ) % {
                    'qty': move.quantity,
                    'uom': move.product_uom.name,
                    'product': move.product_id.display_name,
                    'issued': issued,
                    'ret': already_returned,
                    'out': outstanding,
                })

    def _post_validate_material_issuance(self):
        """Post backcharge journal + liability sheet only for subcontractor + backcharge."""
        self.ensure_one()
        if self.x_is_return_transfer:
            return
        if self.x_issue_type != 'subcontractor' or not self.x_backcharge_applicable:
            return
        if not self.x_contact_id or self.x_backcharge_refund_entry_id:
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

        entry = self._create_issuance_journal_entry(amount, is_return=False)
        if not entry:
            return
        self.x_backcharge_refund_entry_id = entry
        self.message_post(
            body=_('Backcharge entry <b>%s</b> (%.2f) posted to partner ledger.')
            % (entry.name, amount)
        )
        self._auto_update_liability_sheet(amount)

    def _post_validate_return(self):
        """Reverse backcharge on subcontractor returns; post damage charges if needed."""
        self.ensure_one()
        orig = self.x_original_issuance_id
        if not orig:
            return

        if (orig.x_issue_type == 'subcontractor'
                and orig.x_backcharge_applicable
                and not self.x_return_backcharge_entry_id):
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
                        body=_(
                            'Backcharge reversal <b>%s</b> (%.2f) posted to partner ledger.'
                        ) % (entry.name, adj_amount)
                    )
                    self._auto_adjust_liability_sheet_on_return(adj_amount, orig)

        if self.x_return_type == 'damaged' and not self.x_damage_backcharge_entry_id:
            self._post_validate_damage_backcharge(orig)

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
                body=_('Damage backcharge <b>%s</b> (%.2f) posted.') % (
                    entry.name, amount)
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
        self.message_post(
            body=_(
                'Inter-project entry <b>%s</b> created — Receivable on <b>%s</b>, '
                'Payable on <b>%s</b> (%.2f).'
            ) % (move.name, src_project.name, dst_project.name, total_value)
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
                'name': move.product_id.display_name,
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
            body=_(
                'Destination receipt <b>%s</b> created for <b>%s</b>. '
                'Destination site store must validate receipt.'
            ) % (dest_picking.name, self.x_dest_project_id.name)
        )
        dest_picking.message_post(
            body=_('Incoming site-to-site transfer from <b>%s</b> (%s).') % (
                self.x_issuance_project_id.name, self.name)
        )

    # ─────────────────────────────────────────────────────────────────────────
    # ACCOUNTING HELPERS
    # ─────────────────────────────────────────────────────────────────────────

    def _create_issuance_journal_entry(self, amount, is_return=False, original=None):
        """Post a plain journal entry for material issuance / return.

        Issuance:
          DR  Material Issuance Expense  (analytic: project)
          CR  Accounts Payable           (partner → partner ledger)

        Return (reversal):
          DR  Accounts Payable           (partner → partner ledger)
          CR  Material Issuance Expense  (analytic: project)

        Using move_type='entry' avoids the invoice/bill complexity and
        guarantees correct partner-ledger visibility regardless of whether
        the contact has been invoiced before.
        """
        self.ensure_one()
        journal = self._get_or_create_backcharge_journal()
        partner = self.x_contact_id
        analytic_id = self.x_issuance_project_id.id

        Account = self.env['account.account'].sudo()
        # Expense/cost side
        expense_account = Account.search(
            [('account_type', 'in', ['expense', 'expense_direct_cost'])], limit=1)
        # Payable side — prefer partner's own payable account
        payable_account = (
            partner.sudo().property_account_payable_id
            if partner and partner.property_account_payable_id
            else Account.search([('account_type', '=', 'liability_payable')], limit=1)
        )

        if not expense_account or not payable_account:
            self.message_post(body=_(
                'Warning: expense or payable account not found. '
                'Journal entry skipped — configure Chart of Accounts.'))
            return None

        label = (self.x_backcharge_description
                 or (_('Return: %s') % (original.name if original else self.name)
                     if is_return
                     else _('Material Issuance: %s') % self.name))
        analytic = {str(analytic_id): 100} if analytic_id else {}

        if is_return:
            dr_account, cr_account = payable_account, expense_account
            dr_partner = partner.id if partner else False
            cr_partner = False
        else:
            dr_account, cr_account = expense_account, payable_account
            dr_partner = False
            cr_partner = partner.id if partner else False

        move = self.env['account.move'].sudo().create({
            'move_type': 'entry',
            'journal_id': journal.id,
            'ref': label,
            'invoice_date': fields.Date.today(),
            'narration': self.x_backcharge_description or False,
            'line_ids': [
                (0, 0, {
                    'account_id': dr_account.id,
                    'partner_id': dr_partner,
                    'name': label,
                    'debit': amount,
                    'credit': 0.0,
                    'analytic_distribution': analytic,
                }),
                (0, 0, {
                    'account_id': cr_account.id,
                    'partner_id': cr_partner,
                    'name': label,
                    'debit': 0.0,
                    'credit': amount,
                    'analytic_distribution': analytic,
                }),
            ],
        })
        move.action_post()
        return move

    def _create_damage_journal_entry(self, amount, original):
        """Post damage backcharge: Dr Subcontractor Payable, Cr Damage Recovery."""
        self.ensure_one()
        journal = self._get_or_create_backcharge_journal()
        partner = self.x_contact_id or original.x_contact_id
        analytic_id = original.x_issuance_project_id.id
        Account = self.env['account.account'].sudo()
        recovery_account = Account.search(
            [('name', 'ilike', 'Damage Recovery')], limit=1)
        if not recovery_account:
            recovery_account = Account.search(
                [('account_type', 'in', ['income', 'income_other'])], limit=1)
        payable_account = (
            partner.sudo().property_account_payable_id
            if partner and partner.property_account_payable_id
            else Account.search([('account_type', '=', 'liability_payable')], limit=1)
        )
        if not recovery_account or not payable_account:
            self.message_post(body=_(
                'Warning: damage recovery or payable account not found — '
                'damage entry skipped.'))
            return None
        label = _('Damage backcharge — Return %s') % self.name
        analytic = {str(analytic_id): 100} if analytic_id else {}
        move = self.env['account.move'].sudo().create({
            'move_type': 'entry',
            'journal_id': journal.id,
            'ref': label,
            'invoice_date': fields.Date.today(),
            'line_ids': [
                (0, 0, {
                    'account_id': payable_account.id,
                    'partner_id': partner.id if partner else False,
                    'name': label,
                    'debit': amount,
                    'credit': 0.0,
                    'analytic_distribution': analytic,
                }),
                (0, 0, {
                    'account_id': recovery_account.id,
                    'name': label,
                    'debit': 0.0,
                    'credit': amount,
                    'analytic_distribution': analytic,
                }),
            ],
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
                body=_('Liability Sheet <b>%s</b> auto-created for %s.') % (
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
                body=_('Liability Sheet <b>%s</b> updated for <b>%s</b>: +%s (total %s)') % (
                    sheet.name,
                    self.x_contact_id.name,
                    f'{amount:,.2f}',
                    f'{line.new_liability:,.2f}',
                )
            )
        else:
            desc = self.x_backcharge_description or _('Material Issuance - %s') % self.name
            sheet.write({
                'line_ids': [(0, 0, {
                    'description': desc,
                    'partner_id': self.x_contact_id.id,
                    'new_liability': amount,
                    'recommended_amount': amount,
                })]
            })
            self.message_post(
                body=_('Line added to Liability Sheet <b>%s</b>: %s — %s') % (
                    sheet.name, desc, f'{amount:,.2f}')
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
                        body=_(
                            'Liability Sheet <b>%s</b> updated for <b>%s</b>: '
                            'reduced by <b>%s</b>.'
                        ) % (
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
        }

    def action_view_backcharge_entries(self):
        """Open backcharge accounting entries."""
        self.ensure_one()
        entry_ids = []
        if self.x_backcharge_refund_entry_id:
            entry_ids.append(self.x_backcharge_refund_entry_id.id)
        if self.x_return_backcharge_entry_id:
            entry_ids.append(self.x_return_backcharge_entry_id.id)
        if self.x_damage_backcharge_entry_id:
            entry_ids.append(self.x_damage_backcharge_entry_id.id)
        return {
            'name': _('Backcharge Entries'),
            'type': 'ir.actions.act_window',
            'res_model': 'account.move',
            'view_mode': 'list,form',
            'domain': [('id', 'in', entry_ids)],
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

    def action_print_gate_pass(self):
        return self.env.ref(
            'site_operations.action_report_gate_pass').report_action(self)
