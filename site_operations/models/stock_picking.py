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
        string='Generate Gate Pass Outward', default=False)
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
    x_interproject_entry_id = fields.Many2one(
        'account.move', string='Inter-Project Entry', readonly=True)

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
            if not res.get('picking_type_id'):
                user = self.env.user
                pt = None
                if hasattr(user, 'x_default_warehouse_id') and user.x_default_warehouse_id:
                    pt = user.x_default_warehouse_id.int_type_id
                if not pt:
                    pt = self.env['stock.picking.type'].search(
                        [('code', '=', 'internal')], limit=1)
                if pt:
                    res['picking_type_id'] = pt.id
                    if pt.default_location_src_id:
                        res.setdefault('location_id', pt.default_location_src_id.id)
                    if pt.default_location_dest_id:
                        res.setdefault('location_dest_id', pt.default_location_dest_id.id)
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

    # ─────────────────────────────────────────────────────────────────────────
    # CREATE
    # ─────────────────────────────────────────────────────────────────────────

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('x_transfer_purpose') in ('material_issuance', 'site_to_site'):
                user = self.env.user
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
        return super().create(vals_list)

    # ─────────────────────────────────────────────────────────────────────────
    # COMPUTE METHODS
    # ─────────────────────────────────────────────────────────────────────────

    @api.depends('move_ids.x_line_backcharge_amount')
    def _compute_backcharge_amount(self):
        for pick in self:
            pick.x_backcharge_amount = sum(
                pick.move_ids.mapped('x_line_backcharge_amount'))

    @api.depends('x_contact_id', 'x_issuance_project_id')
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
            )
            pick.x_interproject_entry_count = (
                1 if pick.x_interproject_entry_id else 0
            )

    # ─────────────────────────────────────────────────────────────────────────
    # VALIDATION
    # ─────────────────────────────────────────────────────────────────────────

    def button_validate(self):
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
                    pick._post_validate_site_to_site()
                if pick.x_is_return_transfer:
                    pick._post_validate_return()
        return res

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
        """Generate backcharge accounting entry on issuance validation."""
        self.ensure_one()
        if (self.x_backcharge_applicable
                and self.x_inventory_type == 'consumable'
                and self.x_backcharge_amount > 0
                and not self.x_backcharge_refund_entry_id):
            entry = self._create_backcharge_entry(
                self.x_backcharge_amount, is_return=False)
            self.x_backcharge_refund_entry_id = entry
            self.message_post(
                body=_(
                    'Backcharge entry <b>%s</b> created for %s.'
                ) % (entry.name, self.name)
            )
            # Auto-create / update liability sheet for this project
            self._auto_update_liability_sheet(entry)

    def _post_validate_return(self):
        """Generate backcharge return adjustment or asset backcharge on return."""
        self.ensure_one()
        orig = self.x_original_issuance_id
        if not orig:
            return
        # Consumable return with original backcharge
        if (orig.x_backcharge_applicable
                and orig.x_inventory_type == 'consumable'
                and orig.x_backcharge_refund_entry_id):
            orig_qty = sum(orig.move_ids.mapped('quantity')) or 1.0
            ret_qty = sum(self.move_ids.mapped('quantity'))
            proportion = ret_qty / orig_qty
            adj_amount = orig.x_backcharge_amount * proportion
            if adj_amount > 0:
                entry = self._create_backcharge_entry(
                    adj_amount, is_return=True, original=orig)
                self.x_return_backcharge_entry_id = entry
                self.message_post(
                    body=_(
                        'Backcharge Return Adjustment Entry <b>%s</b> created for %s.'
                    ) % (entry.name, orig.name)
                )
                # Reduce the liability sheet line for this issuance
                self._auto_adjust_liability_sheet_on_return(adj_amount, orig)
        # Asset return with backcharge decision
        elif (orig.x_inventory_type == 'asset'
              and self.x_return_backcharge_applicable):
            adj_amount = sum(m.x_line_backcharge_amount for m in self.move_ids)
            if adj_amount > 0:
                entry = self._create_backcharge_entry(
                    adj_amount, is_return=False)
                self.x_return_backcharge_entry_id = entry
                self.message_post(
                    body=_(
                        'Asset Backcharge Entry <b>%s</b> created for %s.'
                    ) % (entry.name, self.name)
                )

    def _post_validate_site_to_site(self):
        """Generate inter-project payable/receivable accounting entry."""
        self.ensure_one()
        if self.x_interproject_entry_id:
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
        aml_vals = [
            {
                'account_id': receivable_account.id,
                'name': _(
                    'Inter-project receivable: %s → %s'
                ) % (src_project.name, dst_project.name),
                'debit': total_value,
                'credit': 0.0,
                'analytic_distribution': {str(src_project.id): 100},
            },
            {
                'account_id': payable_account.id,
                'name': _(
                    'Inter-project payable: %s → %s'
                ) % (src_project.name, dst_project.name),
                'debit': 0.0,
                'credit': total_value,
                'analytic_distribution': {str(dst_project.id): 100},
            },
        ]
        move = self.env['account.move'].sudo().create({
            'move_type': 'entry',
            'journal_id': journal.id,
            'ref': _(
                'Site-to-Site Transfer %s: %s → %s'
            ) % (self.name, src_project.name, dst_project.name),
            'line_ids': [(0, 0, v) for v in aml_vals],
        })
        move.action_post()
        self.x_interproject_entry_id = move
        self.message_post(
            body=_('Inter-project accounting entry <b>%s</b> created.') % move.name
        )

    # ─────────────────────────────────────────────────────────────────────────
    # ACCOUNTING HELPERS
    # ─────────────────────────────────────────────────────────────────────────

    def _create_backcharge_entry(self, amount, is_return=False, original=None):
        """Create a vendor credit note (in_refund) to backcharge the subcontractor,
        or a vendor bill (in_invoice) for a return adjustment."""
        self.ensure_one()
        journal = self._get_or_create_backcharge_journal()
        partner = self.x_contact_id
        if is_return:
            move_type = 'in_invoice'
            ref = _('Backcharge Return Adjustment - %s') % (
                original.name if original else self.name)
        else:
            move_type = 'in_refund'
            ref = _('Backcharge / Material Issuance - %s') % self.name
        bc_account = self._get_or_create_backcharge_account()
        line_name = self.x_backcharge_description or ref
        move = self.env['account.move'].sudo().create({
            'move_type': move_type,
            'partner_id': partner.id if partner else False,
            'journal_id': journal.id,
            'ref': ref,
            'invoice_date': fields.Date.today(),
            'narration': self.x_backcharge_description or False,
            'invoice_line_ids': [(0, 0, {
                'name': line_name,
                'quantity': 1,
                'price_unit': amount,
                'account_id': bc_account.id,
            })],
        })
        move.action_post()
        return move

    def _auto_update_liability_sheet(self, entry):
        """Auto-create or update the current-month liability sheet for this project."""
        self.ensure_one()
        if not self.x_issuance_project_id or not self.x_contact_id:
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
                'new_liability': line.new_liability + self.x_backcharge_amount,
                'recommended_amount': line.recommended_amount + self.x_backcharge_amount,
            })
            self.message_post(
                body=_('Liability Sheet <b>%s</b> updated for <b>%s</b>: +%s (total %s)') % (
                    sheet.name,
                    self.x_contact_id.name,
                    f'{self.x_backcharge_amount:,.2f}',
                    f'{line.new_liability:,.2f}',
                )
            )
        else:
            desc = self.x_backcharge_description or _('Backcharge - %s') % self.name
            sheet.write({
                'line_ids': [(0, 0, {
                    'description': desc,
                    'partner_id': self.x_contact_id.id,
                    'new_liability': self.x_backcharge_amount,
                    'recommended_amount': self.x_backcharge_amount,
                })]
            })
            self.message_post(
                body=_('Line added to Liability Sheet <b>%s</b>: %s — %s') % (
                    sheet.name, desc, f'{self.x_backcharge_amount:,.2f}')
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
                # Match by partner and description referencing the original picking
                if (line.partner_id == original.x_contact_id
                        and original.name in (line.description or '')):
                    new_liability = max(line.new_liability - adj_amount, 0.0)
                    new_recommended = max(line.recommended_amount - adj_amount, 0.0)
                    line.write({
                        'new_liability': new_liability,
                        'recommended_amount': new_recommended,
                    })
                    self.message_post(
                        body=_(
                            'Liability Sheet <b>%s</b> updated: '
                            'line "%s" reduced by <b>%s</b>.'
                        ) % (sheet.name, line.description or '', f'{adj_amount:,.2f}')
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
