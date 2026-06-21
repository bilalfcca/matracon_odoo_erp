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
        'res.partner', string='Contact', tracking=True,
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

    # ── Smart button counts ───────────────────────────────────────────────────
    x_return_count = fields.Integer(
        string='Returns', compute='_compute_return_count', store=False)
    x_backcharge_entry_count = fields.Integer(
        string='Backcharge Entries', compute='_compute_entry_counts', store=False)
    x_interproject_entry_count = fields.Integer(
        string='Inter-Project Entries', compute='_compute_entry_counts', store=False)

    # ─────────────────────────────────────────────────────────────────────────
    # CREATE
    # ─────────────────────────────────────────────────────────────────────────

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if (not vals.get('x_issuance_project_id')
                    and vals.get('x_transfer_purpose') in ('material_issuance', 'site_to_site')):
                user = self.env.user
                if user.x_default_analytic_account_id:
                    vals['x_issuance_project_id'] = user.x_default_analytic_account_id.id
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

    def _compute_return_count(self):
        for pick in self:
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
        move = self.env['account.move'].create({
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
        move = self.env['account.move'].create({
            'move_type': move_type,
            'partner_id': partner.id if partner else False,
            'journal_id': journal.id,
            'ref': ref,
            'invoice_line_ids': [(0, 0, {
                'name': self.x_backcharge_description or ref,
                'quantity': 1,
                'price_unit': amount,
                'account_id': bc_account.id,
            })],
        })
        move.action_post()
        return move

    def _get_or_create_backcharge_journal(self):
        Journal = self.env['account.journal']
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
        Account = self.env['account.account']
        account = Account.search(
            [('name', 'ilike', 'Backcharge'),
             ('account_type', '=', 'liability_payable')], limit=1)
        if not account:
            account = Account.search(
                [('account_type', '=', 'liability_payable')], limit=1)
        return account

    def _get_or_create_interproject_journal(self):
        Journal = self.env['account.journal']
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
        Account = self.env['account.account']
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
