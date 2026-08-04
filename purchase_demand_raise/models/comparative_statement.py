from markupsafe import Markup, escape
from odoo import models, fields, api, Command, _
from odoo.exceptions import UserError


class ComparativeStatement(models.Model):
    _name = 'x.comparative.statement'
    _description = 'Comparative Statement'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'id desc'

    name = fields.Char(string='Reference', required=True, tracking=True)
    x_purchase_order_id = fields.Many2one(
        'purchase.order', string='Purchase Requisition',
        required=True, tracking=True, ondelete='cascade',
    )
    x_state = fields.Selection([
        ('draft', 'Draft'),
        ('confirmed', 'Confirmed'),
    ], default='draft', string='State', tracking=True)

    x_vendor_line_ids = fields.One2many(
        'x.cs.vendor', 'x_cs_id', string='Vendor Comparisons',
    )

    # ── Flat price-entry one2many (all vendor-product lines in one editable list) ─
    x_all_line_ids = fields.One2many(
        'x.cs.vendor.line', 'x_cs_id',
        string='All Quote Lines',
        help='Flat view of every vendor × product line — edit prices here directly.',
    )

    # ── Vendor count (for stat button) ───────────────────────────────────────
    x_vendor_count = fields.Integer(
        string='Vendors', compute='_compute_vendor_count',
    )

    def _compute_vendor_count(self):
        for cs in self:
            cs.x_vendor_count = len(cs.x_vendor_line_ids)

    # ── Winner selection ──────────────────────────────────────────────────────
    x_recommended_vendor_line_id = fields.Many2one(
        'x.cs.vendor', string='Winner',
        domain="[('x_cs_id', '=', id)]",
        tracking=True, copy=False,
        help='The selected winning vendor. '
             'Use the "⭐ Select as Winner" button on a vendor row to set this.',
    )
    x_recommended_vendor_id = fields.Many2one(
        'res.partner', string='Recommended Vendor',
        related='x_recommended_vendor_line_id.x_partner_id',
        store=True, readonly=True, tracking=True,
    )

    x_negotiation_notes = fields.Text(string='Negotiation Notes / Audit Trail')

    # ── Product Summary (computed HTML shown above tabs) ──────────────────────
    x_product_summary_html = fields.Html(
        string='Product Summary',
        compute='_compute_product_summary_html',
        sanitize=False,
    )

    @api.depends(
        'x_purchase_order_id.order_line.product_id',
        'x_purchase_order_id.order_line.x_requested_qty',
        'x_purchase_order_id.order_line.x_recommended_qty',
        'x_purchase_order_id.order_line.x_approved_qty',
        'x_purchase_order_id.order_line.x_qty_on_hand',
    )
    def _compute_product_summary_html(self):
        for cs in self:
            pr = cs.x_purchase_order_id
            if not pr:
                cs.x_product_summary_html = Markup('')
                continue
            lines = pr.order_line.filtered(lambda l: l.product_id and not l.display_type)
            if not lines:
                cs.x_product_summary_html = Markup(
                    '<p class="text-muted fst-italic">No product lines on this PR yet.</p>'
                )
                continue

            _td = 'border:1px solid #dee2e6;padding:6px 10px;vertical-align:middle;'
            _th = ('border:1px solid #1a3a5c;padding:6px 10px;background:#1a3a5c;'
                   'color:#fff;text-align:center;')

            rows = []
            for i, line in enumerate(lines, 1):
                req = line.x_requested_qty or line.product_qty or 0.0
                on_hand = line.x_qty_on_hand or 0.0
                rec = line.x_recommended_qty or 0.0
                appr = line.x_approved_qty or 0.0

                # Warn row if recommended is less than requested
                warn = rec > 0 and rec < req
                row_bg = '#fff8e1' if warn else ('#f9f9f9' if i % 2 else '#fff')

                row_style = 'background:%s;' % row_bg
                rec_style = _td + ('color:#e65100;font-weight:bold;' if warn else '')
                warn_icon = Markup(' ⚠️') if warn else Markup('')

                rows.append(
                    Markup('<tr style="') + Markup(row_style) + Markup('">'
                    '<td style="') + Markup(_td + 'text-align:center;') + Markup('">') + Markup(str(i)) + Markup('</td>'
                    '<td style="') + Markup(_td) + Markup('">') + escape(line.product_id.display_name) + Markup('</td>'
                    '<td style="') + Markup(_td + 'text-align:center;') + Markup('">') + escape(line.product_uom_id.name or '') + Markup('</td>'
                    '<td style="') + Markup(_td + 'text-align:right;') + Markup('">') + Markup('%.4g' % req) + Markup('</td>'
                    '<td style="') + Markup(_td + 'text-align:right;') + Markup('">') + Markup('%.4g' % on_hand) + Markup('</td>'
                    '<td style="') + Markup(rec_style + 'text-align:right;') + Markup('">') + Markup('%.4g' % rec) + warn_icon + Markup('</td>'
                    '<td style="') + Markup(_td + 'text-align:right;') + Markup('">') + Markup('%.4g' % appr if appr else '—') + Markup('</td>'
                    '</tr>')
                )

            html = (
                Markup('<div style="overflow-x:auto;margin:0 0 12px 0;">'
                       '<table style="width:100%;border-collapse:collapse;font-size:13px;">'
                       '<thead><tr>'
                       '<th style="') + Markup(_th + 'width:30px;') + Markup('">#</th>'
                       '<th style="') + Markup(_th) + Markup('">Product</th>'
                       '<th style="') + Markup(_th + 'width:60px;') + Markup('">UoM</th>'
                       '<th style="') + Markup(_th + 'width:100px;') + Markup('">Requested</th>'
                       '<th style="') + Markup(_th + 'width:100px;') + Markup('">On Hand</th>'
                       '<th style="') + Markup(_th + 'width:110px;') + Markup('">Recommended</th>'
                       '<th style="') + Markup(_th + 'width:100px;') + Markup('">Approved</th>'
                       '</tr></thead><tbody>')
                + Markup('').join(rows)
                + Markup('</tbody></table></div>')
            )
            cs.x_product_summary_html = html

    # ── Cross-vendor comparison matrix (computed HTML) ────────────────────────
    x_comparison_html = fields.Html(
        string='Comparison Matrix',
        compute='_compute_comparison_html',
        sanitize=False,
    )

    @api.depends(
        'x_vendor_line_ids.x_partner_id',
        'x_vendor_line_ids.x_is_recommended',
        'x_vendor_line_ids.x_total_price',
        'x_vendor_line_ids.x_line_ids.x_product_id',
        'x_vendor_line_ids.x_line_ids.x_qty',
        'x_vendor_line_ids.x_line_ids.x_unit_price',
        'x_vendor_line_ids.x_line_ids.x_total_price',
    )
    def _compute_comparison_html(self):
        for cs in self:
            vendors = cs.x_vendor_line_ids
            if not vendors:
                cs.x_comparison_html = Markup(
                    '<p class="text-muted fst-italic mt-2 mb-2">'
                    'Add vendors in the <strong>Vendors &amp; Terms</strong> tab '
                    'to see the price comparison matrix here.'
                    '</p>'
                )
                continue

            # Collect all unique products (ordered by first appearance)
            prod_map = {}
            for vendor in vendors:
                for line in vendor.x_line_ids:
                    p = line.x_product_id
                    if p and p.id not in prod_map:
                        prod_map[p.id] = p

            if not prod_map:
                cs.x_comparison_html = Markup(
                    '<p class="text-muted fst-italic mt-2 mb-2">'
                    'Add product lines to each vendor in the '
                    '<strong>Quote Lines</strong> tab to populate this matrix.'
                    '</p>'
                )
                continue

            # Build vendor → product → line lookup
            vp_map = {}
            for vendor in vendors:
                vp_map[vendor.id] = {}
                for line in vendor.x_line_ids:
                    if line.x_product_id:
                        vp_map[vendor.id][line.x_product_id.id] = line

            # ── Build HTML table ──────────────────────────────────────────────
            parts = []
            parts.append(Markup(
                '<div style="overflow-x:auto;margin-top:4px;">'
                '<table style="width:100%;border-collapse:collapse;font-size:11px;">'
                '<thead>'
            ))

            # ── Vendor header row ─────────────────────────────────────────────
            parts.append(Markup('<tr>'))
            _hdr_base = 'border:1px solid #1a3a5c;padding:6px 10px;color:#fff;'
            _hdr_reg = _hdr_base + 'background:#1a3a5c;text-align:center;'
            # Winner: gold border, green background, larger font for clear visual emphasis
            _hdr_win = (
                _hdr_base
                + 'background:#0a5c2a;text-align:center;'
                + 'font-weight:bold;font-size:14px;'
                + 'border:3px solid #ffc107;'
                + 'box-shadow:inset 0 0 0 2px #ffc107;'
            )

            for label in ('#', 'Product', 'UoM', 'Qty'):
                parts.append(
                    Markup('<th rowspan="2" style="') + Markup(_hdr_reg) + Markup('">')
                    + Markup(label)
                    + Markup('</th>')
                )

            for vendor in vendors:
                if vendor.x_is_recommended:
                    style = _hdr_win
                    label_html = (
                        Markup('<div style="font-size:16px;line-height:1;">&#127942;</div>')
                        + Markup('<div style="font-size:14px;font-weight:bold;letter-spacing:0.3px;">')
                        + escape(vendor.x_partner_id.name or 'Vendor')
                        + Markup('</div>')
                        + Markup('<div style="font-size:10px;font-weight:normal;opacity:0.9;">'
                                 'SELECTED WINNER</div>')
                    )
                else:
                    style = _hdr_reg
                    label_html = escape(vendor.x_partner_id.name or 'Vendor')
                parts.append(
                    Markup('<th colspan="2" style="') + Markup(style) + Markup('">')
                    + label_html
                    + Markup('</th>')
                )
            parts.append(Markup('</tr>'))

            # ── Rate / Total sub-header row ───────────────────────────────────
            parts.append(Markup('<tr>'))
            for vendor in vendors:
                style = _hdr_win if vendor.x_is_recommended else _hdr_reg
                sub_style = style + 'font-size:11px;'
                parts.append(
                    Markup('<th style="') + Markup(sub_style) + Markup('">Rate</th>')
                )
                parts.append(
                    Markup('<th style="') + Markup(sub_style) + Markup('">Total</th>')
                )
            parts.append(Markup('</tr></thead><tbody>'))

            # ── Product rows ──────────────────────────────────────────────────
            _td = 'border:1px solid #ccc;padding:4px 7px;'
            _tdr = _td + 'text-align:right;'
            _tdc = _td + 'text-align:center;'

            for seq, (prod_id, product) in enumerate(prod_map.items(), 1):
                # Grab qty/uom from first vendor that has this product
                qty = 0.0
                uom_name = ''
                for vendor in vendors:
                    line = vp_map[vendor.id].get(prod_id)
                    if line:
                        qty = line.x_qty
                        uom_name = line.x_uom_id.name or ''
                        break

                # Lowest total for highlight
                totals = [
                    vp_map[v.id][prod_id].x_total_price
                    for v in vendors
                    if prod_id in vp_map[v.id] and vp_map[v.id][prod_id].x_total_price > 0
                ]
                min_price = min(totals) if totals else 0.0

                # Row background alternating
                row_bg = '#ffffff' if seq % 2 else '#f9f9f9'
                parts.append(Markup('<tr style="background:') + Markup(row_bg) + Markup(';">'))

                parts.append(Markup('<td style="') + Markup(_tdc) + Markup('">')
                             + Markup(str(seq)) + Markup('</td>'))
                parts.append(Markup('<td style="') + Markup(_td) + Markup('">')
                             + escape(product.display_name) + Markup('</td>'))
                parts.append(Markup('<td style="') + Markup(_tdc) + Markup('">')
                             + escape(uom_name) + Markup('</td>'))
                parts.append(Markup('<td style="') + Markup(_tdr) + Markup('">')
                             + Markup('%.3g' % qty) + Markup('</td>'))

                for vendor in vendors:
                    line = vp_map[vendor.id].get(prod_id)
                    if line and (line.x_unit_price or line.x_total_price):
                        is_best = min_price > 0 and abs(line.x_total_price - min_price) < 0.001
                        if is_best:
                            cell_bg = '#c3e6cb'
                            fw = 'bold'
                        elif vendor.x_is_recommended:
                            cell_bg = '#e8f5e9'
                            fw = 'normal'
                        else:
                            cell_bg = row_bg
                            fw = 'normal'
                        cell_style = (
                            _tdr
                            + 'background:' + cell_bg + ';'
                            + 'font-weight:' + fw + ';'
                        )
                        parts.append(
                            Markup('<td style="') + Markup(cell_style) + Markup('">')
                            + Markup('{:,.2f}'.format(line.x_unit_price))
                            + Markup('</td>')
                        )
                        parts.append(
                            Markup('<td style="') + Markup(cell_style) + Markup('">')
                            + Markup('{:,.2f}'.format(line.x_total_price))
                            + Markup('</td>')
                        )
                    else:
                        parts.append(
                            Markup('<td colspan="2" style="') + Markup(_tdc)
                            + Markup('" style="color:#999;font-style:italic;">'
                                     '<span style="color:#bbb;">— not quoted —</span></td>')
                        )
                parts.append(Markup('</tr>'))

            # ── Footer totals ─────────────────────────────────────────────────
            parts.append(Markup(
                '<tfoot>'
                '<tr style="font-weight:bold;border-top:2px solid #333;">'
            ))
            parts.append(
                Markup('<td colspan="4" style="') + Markup(_tdr)
                + Markup('background:#e8f0fe;">GRAND TOTAL</td>')
            )
            for vendor in vendors:
                foot_bg = '#c3e6cb' if vendor.x_is_recommended else '#e8f0fe'
                foot_style = _tdr + 'background:' + foot_bg + ';font-weight:bold;'
                parts.append(
                    Markup('<td colspan="2" style="') + Markup(foot_style) + Markup('">')
                    + Markup('{:,.2f}'.format(vendor.x_total_price))
                    + Markup('</td>')
                )
            parts.append(Markup('</tr></tfoot></table></div>'))

            cs.x_comparison_html = Markup('').join(parts)

    # ── Actions ───────────────────────────────────────────────────────────────

    def action_confirm(self):
        """Lock CS and route PR to CEO via the HO-approval path."""
        for cs in self:
            if not cs.x_recommended_vendor_line_id:
                raise UserError(_(
                    'Please select a winning vendor first.\n\n'
                    'Click "⭐ Choose" on the vendor row to select the winner.'
                ))
            if not cs.x_recommended_vendor_line_id.x_partner_id:
                raise UserError(_(
                    'The selected winning vendor must have a partner set.'
                ))

            cs.x_state = 'confirmed'

            if cs.x_purchase_order_id:
                cs.x_purchase_order_id.action_confirm_cs(cs)

            cs.message_post(
                body=_(
                    'Comparative Statement confirmed. '
                    'Selected vendor: %s.'
                ) % cs.x_recommended_vendor_line_id.x_partner_id.name,
                subtype_xmlid='mail.mt_log_note',
            )

    def action_print(self):
        """Print CS PDF report."""
        self.ensure_one()
        return self.env.ref(
            'purchase_demand_raise.action_report_cs_from_cs'
        ).report_action(self)

    def action_preview_cs(self):
        """Open CS as formatted HTML page in the browser (for PO/CEO review)."""
        self.ensure_one()
        return self.env.ref(
            'purchase_demand_raise.action_report_cs_preview_html'
        ).report_action(self)

    def action_send_rfq_to_vendors(self):
        """Open mail compose dialog to send RFQ to ALL vendors in this CS.

        Opens a single mail-compose window pre-filled with:
          • Our custom Matracon RFQ email template
          • All vendor partners from x_vendor_line_ids (with emails) as recipients
          • The linked PR as the compose-target record (template renders against it)
          • The Matracon custom RFQ PDF attached
        """
        self.ensure_one()
        po = self.x_purchase_order_id
        if not po:
            raise UserError(_('No Purchase Requisition linked to this Comparative Statement.'))

        # Collect vendor partners that have an email address
        partners_with_email = self.x_vendor_line_ids.mapped('x_partner_id').filtered(
            lambda p: p.email
        )
        if not partners_with_email:
            raise UserError(_(
                'No vendor email addresses found in this Comparative Statement.\n\n'
                'Please add email addresses to the vendor contacts first.'
            ))

        # Use our standalone Matracon RFQ template (has our formal body AND
        # our custom RFQ PDF in report_template_ids — no noupdate conflict)
        template = self.env.ref(
            'purchase_demand_raise.matracon_rfq_email_template',
            raise_if_not_found=False,
        )

        ctx = {
            'default_model': 'purchase.order',
            'default_res_ids': po.ids,
            'default_composition_mode': 'comment',
            'default_partner_ids': partners_with_email.ids,
            'default_email_layout_xmlid': 'mail.mail_notification_layout_with_responsible_signature',
            'force_email': True,
            'send_rfq': True,
        }
        if template:
            ctx['default_template_id'] = template.id

        return {
            'type': 'ir.actions.act_window',
            'name': _('Send RFQ to Vendors'),
            'res_model': 'mail.compose.message',
            'view_mode': 'form',
            'target': 'new',
            'context': ctx,
        }


class CSVendor(models.Model):

    _name = 'x.cs.vendor'
    _description = 'CS Vendor Comparison'
    _order = 'sequence, id'

    # ── Display name (used when this record is shown as a Many2one) ───────────
    name = fields.Char(
        string='Vendor Name',
        compute='_compute_name',
        store=False,
    )

    @api.depends('x_partner_id')
    def _compute_name(self):
        for record in self:
            record.name = record.x_partner_id.name or _('Unknown Vendor')

    x_cs_id = fields.Many2one(
        'x.comparative.statement', string='CS', ondelete='cascade', index=True,
    )
    sequence = fields.Integer(string='Sequence', default=10)

    x_partner_id = fields.Many2one(
        'res.partner', string='Vendor', required=True,
        domain="[('category_id.name', '=', 'Vendor')]",
    )
    x_is_recommended = fields.Boolean(
        string='Winner', default=False, copy=False,
        help='Mark this vendor as the selected winner.',
    )

    # ── Commercial terms (7 required CS fields) ──────────────────────────────
    # Each field has a configurable Many2one quick-pick AND a Char manual-entry.
    # Picking from the dropdown auto-fills the Char; the Char is the actual
    # stored value used in reports and auto-copied to PO Quotation Terms.
    # Options are managed via Purchase → Configuration → CS Commercial Options.

    # 1. Delivery Basis
    x_delivery_basis_opt_id = fields.Many2one(
        'x.cs.commercial.option',
        string='Delivery Basis',
        domain=[('category', '=', 'delivery_basis')],
        ondelete='set null',
    )
    x_delivery_basis = fields.Char(
        string='↳ Custom',
        help='Final delivery basis — auto-filled by dropdown or type manually.',
    )

    # 2. Taxes & Duties
    x_tax_opt_id = fields.Many2one(
        'x.cs.commercial.option',
        string='Taxes & Duties',
        domain=[('category', '=', 'tax_type')],
        ondelete='set null',
    )
    x_tax_treatment = fields.Char(string='↳ Custom')

    # 3. Payment Mode
    x_payment_mode_opt_id = fields.Many2one(
        'x.cs.commercial.option',
        string='Payment Mode',
        domain=[('category', '=', 'payment_mode')],
        ondelete='set null',
    )
    x_payment_mode = fields.Char(string='↳ Custom')

    # 4. Payment Terms — Odoo native dropdown
    x_payment_term_id = fields.Many2one(
        'account.payment.term',
        string='Payment Terms',
        help='Select from standard payment terms. Choose "Other / Custom Terms" to enter custom text.',
    )
    x_custom_payment_note = fields.Char(
        string='Custom Payment Note',
        help='Specify custom payment arrangement (shown when "Other / Custom Terms" is selected).',
    )

    # 5. Delivery Time
    x_delivery_time_opt_id = fields.Many2one(
        'x.cs.commercial.option',
        string='Delivery Time',
        domain=[('category', '=', 'delivery_time')],
        ondelete='set null',
    )
    x_delivery_period = fields.Char(string='↳ Custom')

    # 6. Brand / Origin
    x_brand_opt_id = fields.Many2one(
        'x.cs.commercial.option',
        string='Brand / Origin',
        domain=[('category', '=', 'brand')],
        ondelete='set null',
    )
    x_brand_origin = fields.Char(string='↳ Custom')

    # 7. Warranty
    x_warranty_opt_id = fields.Many2one(
        'x.cs.commercial.option',
        string='Warranty',
        domain=[('category', '=', 'warranty')],
        ondelete='set null',
    )
    x_warranty = fields.Char(string='↳ Custom')

    # Legacy / retained for existing data
    x_rfq_reference = fields.Char(string='Quotation Reference')
    x_quote_validity = fields.Date(string='Quote Validity')
    x_payment_terms = fields.Char(string='Custom Payment Notes')
    x_ancillary_included = fields.Boolean(string='Ancillary Items Included?')
    x_remarks = fields.Text(string='Remarks')

    # ── T&C Template (per vendor) ─────────────────────────────────────────────
    x_tc_template_id = fields.Many2one(
        'x.po.terms.template',
        string='T&C Template',
        domain="[('active', '=', True)]",
        help='Select a template to auto-fill the Terms & Conditions text below.',
    )
    x_tc_text = fields.Html(
        string='Terms & Conditions',
        sanitize_attributes=False,
        help='Full terms for this vendor. Auto-filled when a template is selected.',
    )

    @api.onchange('x_partner_id')
    def _onchange_partner_populate_lines(self):
        """
        Real-time product line population.

        Fires when the vendor is selected (or changed) in the sub-form dialog.
        If there are no product lines yet, auto-creates them from the linked PR
        so the user can see and price the products immediately without saving first.
        """
        if not self.x_partner_id or self.x_line_ids:
            return
        cs = self.x_cs_id
        if not cs or not cs.x_purchase_order_id:
            return
        pr = cs.x_purchase_order_id
        lines = []
        for pr_line in pr.order_line.filtered(lambda l: l.product_id and not l.display_type):
            qty = (
                pr_line.x_approved_qty
                or pr_line.x_recommended_qty
                or pr_line.x_requested_qty
                or pr_line.product_qty
                or 1.0
            )
            lines.append(Command.create({
                'x_product_id': pr_line.product_id.id,
                'x_qty': qty,
                'x_unit_price': 0.0,
                'x_pr_line_id': pr_line.id,
            }))
        if lines:
            self.x_line_ids = lines

    # ── Dropdown → Char auto-fill onchanges ──────────────────────────────────

    @api.onchange('x_delivery_basis_opt_id')
    def _onchange_delivery_basis_opt(self):
        if self.x_delivery_basis_opt_id:
            self.x_delivery_basis = self.x_delivery_basis_opt_id.name

    @api.onchange('x_tax_opt_id')
    def _onchange_tax_opt(self):
        if self.x_tax_opt_id:
            self.x_tax_treatment = self.x_tax_opt_id.name

    @api.onchange('x_payment_mode_opt_id')
    def _onchange_payment_mode_opt(self):
        if self.x_payment_mode_opt_id:
            self.x_payment_mode = self.x_payment_mode_opt_id.name

    @api.onchange('x_delivery_time_opt_id')
    def _onchange_delivery_time_opt(self):
        if self.x_delivery_time_opt_id:
            self.x_delivery_period = self.x_delivery_time_opt_id.name

    @api.onchange('x_brand_opt_id')
    def _onchange_brand_opt(self):
        if self.x_brand_opt_id:
            self.x_brand_origin = self.x_brand_opt_id.name

    @api.onchange('x_warranty_opt_id')
    def _onchange_warranty_opt(self):
        if self.x_warranty_opt_id:
            self.x_warranty = self.x_warranty_opt_id.name

    @api.onchange('x_tc_template_id')
    def _onchange_tc_template(self):
        """Fires in the sub-form dialog when the template is selected."""
        if self.x_tc_template_id:
            self.x_tc_text = self.x_tc_template_id.content

    def write(self, vals):
        """Ensure x_tc_text is populated from template on write (even via list inline)."""
        if vals.get('x_tc_template_id') and 'x_tc_text' not in vals:
            tmpl = self.env['x.po.terms.template'].browse(vals['x_tc_template_id'])
            if tmpl.content:
                vals['x_tc_text'] = tmpl.content
        return super().write(vals)

    # ── Document Attachments ──────────────────────────────────────────────────
    x_attachment_ids = fields.Many2many(
        'ir.attachment', string='Vendor Quotation Documents',
        relation='x_cs_vendor_attachment_rel',
        column1='vendor_id', column2='attachment_id',
    )

    x_line_ids = fields.One2many('x.cs.vendor.line', 'x_cs_vendor_id', string='Line Items')

    # Computed totals
    x_total_price = fields.Float(
        string='Total Price', compute='_compute_totals', store=True,
    )
    x_gst_amount = fields.Float(
        string='GST Amount', compute='_compute_totals', store=True,
    )
    x_net_total = fields.Float(
        string='Net Total', compute='_compute_totals', store=True,
    )
    x_is_lowest = fields.Boolean(
        string='Best Value', compute='_compute_is_lowest', store=True,
    )
    x_savings_vs_highest = fields.Float(
        string='Saving vs Most Expensive', compute='_compute_savings', store=True,
    )

    @api.depends('x_line_ids.x_total_price', 'x_line_ids.x_gst_amount')
    def _compute_totals(self):
        for vendor in self:
            vendor.x_total_price = sum(vendor.x_line_ids.mapped('x_total_price'))
            vendor.x_gst_amount = sum(vendor.x_line_ids.mapped('x_gst_amount'))
            vendor.x_net_total = vendor.x_total_price + vendor.x_gst_amount

    @api.depends('x_cs_id.x_vendor_line_ids.x_total_price')
    def _compute_is_lowest(self):
        for vendor in self:
            others = vendor.x_cs_id.x_vendor_line_ids
            if others:
                prices = [v.x_total_price for v in others if v.x_total_price > 0]
                min_price = min(prices) if prices else 0.0
                vendor.x_is_lowest = (
                    vendor.x_total_price > 0 and vendor.x_total_price == min_price
                )
            else:
                vendor.x_is_lowest = False

    @api.depends('x_cs_id.x_vendor_line_ids.x_total_price', 'x_total_price')
    def _compute_savings(self):
        for vendor in self:
            others = vendor.x_cs_id.x_vendor_line_ids
            if others:
                max_price = max(others.mapped('x_total_price') or [0])
                vendor.x_savings_vs_highest = max_price - vendor.x_total_price
            else:
                vendor.x_savings_vs_highest = 0.0

    # ── Winner selection & vendor copy ───────────────────────────────────────

    def action_select_as_recommended(self):
        """Mark this vendor as the CS winner; unmark all others."""
        self.ensure_one()
        cs = self.x_cs_id
        if cs.x_state == 'confirmed':
            raise UserError(_('Cannot change the winner on a confirmed Comparative Statement.'))
        cs.x_vendor_line_ids.write({'x_is_recommended': False})
        self.x_is_recommended = True
        cs.x_recommended_vendor_line_id = self
        return True

    def action_print_rfq(self):
        """Print the RFQ PDF for THIS specific vendor (from Comparative Statement).

        Passes the vendor partner ID via with_context() so it survives the
        browser → PDF-renderer round-trip (context IS forwarded; data= is not).
        The template reads env.context.get('rfq_vendor_partner_id') and renders
        this vendor's details at the top of the document.
        """
        self.ensure_one()
        po = self.x_cs_id.x_purchase_order_id
        if not po:
            raise UserError(_('No Purchase Requisition linked to this Comparative Statement.'))
        if not self.x_partner_id:
            raise UserError(_('No vendor selected on this line.'))

        report = self.env.ref('purchase_demand_raise.action_report_rfq_demand_raise')
        return report.with_context(
            rfq_vendor_partner_id=self.x_partner_id.id
        ).report_action(po)

    def action_enter_prices(self):
        """Open the price-entry form for this vendor as a full-page view.

        Uses target='current' so all table columns (including Remarks) are
        fully visible without the width constraint of a modal dialog.
        x_line_ids is at 1-level nesting in the standalone form, so Odoo 19
        saves it reliably — no 3-level nesting issue.
        """
        self.ensure_one()
        if not isinstance(self.id, int) or not self.id:
            raise UserError(_(
                'Please save the Comparative Statement first, '
                'then click "📋 Prices" to enter prices for this vendor.'
            ))
        view = self.env.ref('purchase_demand_raise.view_cs_vendor_price_form')
        return {
            'type': 'ir.actions.act_window',
            'name': _('Prices — %s') % (self.x_partner_id.name or _('Vendor')),
            'res_model': 'x.cs.vendor',
            'res_id': self.id,
            'view_mode': 'form',
            'views': [(view.id, 'form')],
            'target': 'current',
        }

    def action_back_to_cs(self):
        """Navigate back to the parent Comparative Statement form."""
        self.ensure_one()
        cs = self.x_cs_id
        if not cs:
            return {'type': 'ir.actions.act_window_close'}
        return {
            'type': 'ir.actions.act_window',
            'name': cs.name,
            'res_model': 'x.comparative.statement',
            'res_id': cs.id,
            'view_mode': 'form',
            'target': 'current',
        }

    def action_send_rfq(self):
        """Open mail compose dialog to send RFQ to THIS specific vendor."""
        self.ensure_one()
        po = self.x_cs_id.x_purchase_order_id
        if not po:
            raise UserError(_('No Purchase Requisition linked to this Comparative Statement.'))
        if not self.x_partner_id:
            raise UserError(_('No vendor selected on this line.'))
        if not self.x_partner_id.email:
            raise UserError(_(
                'Vendor "%s" does not have an email address.\n\n'
                'Please add an email address to this vendor\'s contact record first.'
            ) % self.x_partner_id.name)

        # Use our standalone Matracon RFQ template (correct PDF, no noupdate conflict)
        template = self.env.ref(
            'purchase_demand_raise.matracon_rfq_email_template',
            raise_if_not_found=False,
        )

        ctx = {
            'default_model': 'purchase.order',
            'default_res_ids': po.ids,
            'default_composition_mode': 'comment',
            'default_partner_ids': [self.x_partner_id.id],
            'default_email_layout_xmlid': 'mail.mail_notification_layout_with_responsible_signature',
            'force_email': True,
            'send_rfq': True,
        }
        if template:
            ctx['default_template_id'] = template.id

        return {
            'type': 'ir.actions.act_window',
            'name': _('Send RFQ — %s') % self.x_partner_id.name,
            'res_model': 'mail.compose.message',
            'view_mode': 'form',
            'target': 'new',
            'context': ctx,
        }

    def action_choose_vendor(self):
        """Choose this vendor: mark as winner AND copy vendor + all terms to the PR."""
        self.ensure_one()
        cs = self.x_cs_id
        if cs.x_state == 'confirmed':
            raise UserError(_('Cannot change the selection on a confirmed Comparative Statement.'))

        # Safety net: ensure ALL vendors in this CS have product lines.
        # Lines can go missing if the Owl client dropped readonly fields during
        # save.  Repopulate any vendor that has no lines before we proceed.
        self.env['x.cs.vendor.line'].flush_model()
        for vendor in cs.x_vendor_line_ids:
            vendor.invalidate_recordset(['x_line_ids'])
            if not vendor.x_line_ids and cs.x_purchase_order_id:
                vendor._auto_create_lines_from_pr()

        # Mark as winner
        cs.x_vendor_line_ids.write({'x_is_recommended': False})
        self.x_is_recommended = True
        cs.x_recommended_vendor_line_id = self

        # Copy vendor and all CS commercial terms to the main PR
        pr = cs.x_purchase_order_id
        if pr:
            vals = {'partner_id': self.x_partner_id.id}
            # Payment Terms (standard Odoo dropdown)
            if self.x_payment_term_id:
                vals['payment_term_id'] = self.x_payment_term_id.id
            # Custom payment note (when "Other / Custom Terms" is selected)
            if self.x_custom_payment_note:
                vals['x_quote_payment_terms'] = self.x_custom_payment_note
            # T&C (if any set on the vendor row — kept for backward compat)
            if self.x_tc_text:
                vals['note'] = self.x_tc_text
            if self.x_tc_template_id:
                vals['x_terms_template_id'] = self.x_tc_template_id.id
            # ── Auto-fill the 7 CS commercial fields into PO Quotation Terms ──
            # These populate the "Quotation Terms (CS)" tab on the PO form.
            if self.x_brand_origin:
                vals['x_quote_brand'] = self.x_brand_origin
            if self.x_delivery_basis:
                vals['x_quote_delivery_basis'] = self.x_delivery_basis
            if self.x_delivery_period:
                vals['x_quote_delivery_period'] = self.x_delivery_period
            if self.x_payment_mode:
                vals['x_quote_payment_terms'] = self.x_payment_mode
            if self.x_tax_treatment:
                vals['x_quote_tax_treatment'] = self.x_tax_treatment
            if self.x_warranty:
                vals['x_quote_warranty'] = self.x_warranty
            pr.sudo().write(vals)

            # Copy winning prices (and per-line currency) to PR lines
            for cs_line in self.x_line_ids:
                if not cs_line.x_product_id:
                    continue
                pr_line = pr.order_line.filtered(
                    lambda l, p=cs_line.x_product_id: l.product_id == p and not l.display_type
                )[:1]
                if pr_line and cs_line.x_unit_price:
                    pr_line.price_unit = cs_line.x_unit_price
                if pr_line and cs_line.x_currency_id:
                    pr_line.x_line_currency_id = cs_line.x_currency_id

            pr.message_post(
                body=Markup(
                    '🏆 <b>Vendor Selected:</b> <b>%(vendor)s</b> chosen from '
                    'Comparative Statement <b>%(cs)s</b>.<br/>'
                    'Terms and price copied to this PR. '
                    'Click <b>Confirm Selection &amp; Route to CEO</b> on the CS to finalise.'
                ) % {'vendor': self.x_partner_id.name, 'cs': cs.name},
                subtype_xmlid='mail.mt_log_note',
            )
        return True

    # ── Pre-populate product lines when dialog opens (before save) ───────────

    @api.model
    def default_get(self, fields_list):
        """Pre-populate x_line_ids from the PR when opening the new-vendor dialog."""
        res = super().default_get(fields_list)
        cs_id = self.env.context.get('default_x_cs_id')
        if cs_id and 'x_line_ids' in fields_list:
            cs = self.env['x.comparative.statement'].browse(cs_id)
            pr = cs.x_purchase_order_id
            if pr:
                lines = []
                for pr_line in pr.order_line.filtered(
                    lambda l: l.product_id and not l.display_type
                ):
                    # Priority: approved → recommended → requested → product_qty
                    qty = (
                        pr_line.x_approved_qty
                        or pr_line.x_recommended_qty
                        or pr_line.x_requested_qty
                        or pr_line.product_qty
                        or 1.0
                    )
                    lines.append(Command.create({
                        'x_product_id': pr_line.product_id.id,
                        'x_qty': qty,
                        'x_unit_price': 0.0,
                        'x_pr_line_id': pr_line.id,
                    }))
                if lines:
                    res['x_line_ids'] = lines
        return res

    # ── Auto-create product lines from PR on vendor addition ──────────────────

    @api.model_create_multi
    def create(self, vals_list):
        """
        1. Syncs x_tc_text from template if not already provided.
        2. Always ensures every new vendor has one product line per PR product.

        Root cause of the "products disappear" bug (Odoo 19):
        The Owl web client strips readonly fields from nested one2many create
        payloads, so x_product_id (readonly="1" in the view) is never sent.
        CSVendorLine.create() then drops those lines.  The old guard
        `if vals.get('x_line_ids'): continue` prevented _auto_create_lines_from_pr()
        from running as a recovery step.

        Fix: always call _auto_create_lines_from_pr() after create.
        It is idempotent — it skips products that already have a line —
        so no duplicates arise when the client DID send lines correctly.
        """
        # ── Step 1: T&C text sync ─────────────────────────────────────────────
        for vals in vals_list:
            if vals.get('x_tc_template_id') and not vals.get('x_tc_text'):
                tmpl = self.env['x.po.terms.template'].browse(vals['x_tc_template_id'])
                if tmpl.exists() and tmpl.content:
                    vals['x_tc_text'] = tmpl.content

        # ── Step 2: Create records ────────────────────────────────────────────
        records = super().create(vals_list)

        # ── Step 3: Always ensure product lines exist from PR ─────────────────
        # Flush so that any x_line_ids already created by super().create() are
        # visible in DB before _auto_create_lines_from_pr() checks existing lines.
        self.env['x.cs.vendor.line'].flush_model()
        for record in records:
            if record.x_cs_id and record.x_cs_id.x_purchase_order_id:
                record._auto_create_lines_from_pr()

        return records

    def write(self, vals):
        """Repopulate product lines from PR if x_line_ids was included in the
        write payload but the vendor ends up with no lines.  This guards against
        the web client sending an empty or incomplete x_line_ids payload (e.g.
        when a button is clicked inside the dialog before any price is entered).
        """
        res = super().write(vals)
        if 'x_line_ids' in vals:
            self.env['x.cs.vendor.line'].flush_model()
            for record in self:
                record.invalidate_recordset(['x_line_ids'])
                if not record.x_line_ids and record.x_cs_id and record.x_cs_id.x_purchase_order_id:
                    record._auto_create_lines_from_pr()
        return res

    def _auto_create_lines_from_pr(self):
        """Create x.cs.vendor.line for each PR product not yet on this vendor.

        Idempotent: skips products that already have a line (by x_product_id).
        Lines that exist but have x_product_id = False are ignored so they are
        overwritten correctly.
        """
        self.ensure_one()
        pr = self.x_cs_id.x_purchase_order_id
        if not pr:
            return
        self.env['x.cs.vendor.line'].flush_model()
        self.invalidate_recordset(['x_line_ids'])
        # Only count lines that actually have a product set.
        existing_products = self.x_line_ids.filtered('x_product_id').mapped('x_product_id')
        to_create = []
        for pr_line in pr.order_line.filtered(lambda l: l.product_id and not l.display_type):
            if pr_line.product_id in existing_products:
                continue
            qty = (
                pr_line.x_approved_qty
                or pr_line.x_recommended_qty
                or pr_line.x_requested_qty
                or pr_line.product_qty
                or 1.0
            )
            to_create.append({
                'x_cs_vendor_id': self.id,
                'x_product_id': pr_line.product_id.id,
                'x_qty': qty,
                'x_unit_price': 0.0,
                'x_pr_line_id': pr_line.id,
            })
        if to_create:
            self.env['x.cs.vendor.line'].create(to_create)


class CSVendorLine(models.Model):
    _name = 'x.cs.vendor.line'
    _description = 'CS Vendor Line Item'
    _order = 'x_cs_vendor_id, id'

    x_cs_vendor_id = fields.Many2one('x.cs.vendor', string='Vendor', ondelete='cascade', index=True)
    # Stored related — enables the flat x_all_line_ids one2many on ComparativeStatement
    x_cs_id = fields.Many2one(
        'x.comparative.statement',
        related='x_cs_vendor_id.x_cs_id',
        store=True, index=True, readonly=True,
    )
    # Link to original PR line (for traceability — set during auto-creation)
    x_pr_line_id = fields.Many2one(
        'purchase.order.line', string='PR Line',
        ondelete='set null', copy=False,
        help='Original PR line this quote line was created from.',
    )
    # required=False at ORM level to avoid ValidationError when the editable
    # list has a pending empty row during form auto-save (triggered by button clicks).
    # required="1" is enforced in the view XML for normal UX validation.
    x_product_id = fields.Many2one('product.product', string='Product', required=False)
    x_uom_id = fields.Many2one('uom.uom', string='UoM', related='x_product_id.uom_id', readonly=True)

    # ── Quantity: always pulled from the PR line's recommended qty ────────────
    # store=True so totals can trigger off it; readonly=False allows override if needed.
    # With store=True the compute recalculates when PR line qtys change, keeping it fresh.
    x_qty = fields.Float(
        string='Quantity', digits='Product Unit of Measure',
        compute='_compute_qty_from_pr', store=True, readonly=False,
    )

    @api.depends(
        'x_pr_line_id',
        'x_pr_line_id.x_recommended_qty',
        'x_pr_line_id.x_requested_qty',
        'x_pr_line_id.product_qty',
    )
    def _compute_qty_from_pr(self):
        for line in self:
            pr = line.x_pr_line_id
            if pr:
                # Priority: recommended → requested → product_qty
                line.x_qty = (
                    pr.x_recommended_qty
                    or pr.x_requested_qty
                    or pr.product_qty
                    or 1.0
                )
            else:
                # No PR link (manually added line) — default to 1.0
                # NOTE: cannot read the field being computed; always assign a value
                line.x_qty = 1.0

    x_currency_id = fields.Many2one(
        'res.currency',
        string='Currency',
        default=lambda self: self.env.company.currency_id,
        help='Currency vendor quoted in. Copied to PO line when this vendor is chosen.',
    )
    x_unit_price = fields.Float(string='Unit Price', digits='Product Price')
    x_total_price = fields.Float(
        string='Total Price', compute='_compute_line_totals', store=True,
    )
    x_gst_rate = fields.Float(string='GST %', default=0.0)
    x_gst_amount = fields.Float(
        string='GST Amount', compute='_compute_line_totals', store=True,
    )
    x_net_price = fields.Float(
        string='Net Price', compute='_compute_line_totals', store=True,
    )
    x_remarks = fields.Char(string='Remarks')

    @api.depends('x_qty', 'x_unit_price', 'x_gst_rate')
    def _compute_line_totals(self):
        for line in self:
            line.x_total_price = line.x_qty * line.x_unit_price
            line.x_gst_amount = line.x_total_price * (line.x_gst_rate / 100.0)
            line.x_net_price = line.x_total_price + line.x_gst_amount

    @api.model_create_multi
    def create(self, vals_list):
        """Ensure every line has a product before writing to the DB.

        Two causes of missing x_product_id:
        1. Odoo 19 web client strips readonly fields from nested one2many create
           payloads — x_product_id (readonly="1") is dropped but x_pr_line_id
           (column_invisible="1", editable from server side) is kept.
           Recovery: look up the product from the linked PR line.
        2. A genuinely blank row auto-saved before the user selected a product
           (e.g. clicking a header button in an editable list).
           Action: silently skip — _auto_create_lines_from_pr() on the parent
           vendor will create the correct lines afterwards.
        """
        cleaned = []
        for v in vals_list:
            if not v.get('x_product_id'):
                pr_line_id = v.get('x_pr_line_id')
                if pr_line_id:
                    # Recover product from the linked PR line (handles Odoo 19
                    # readonly-field stripping in sub-form dialog payloads).
                    prl = self.env['purchase.order.line'].browse(pr_line_id)
                    if prl.exists() and prl.product_id:
                        v = dict(v)
                        v['x_product_id'] = prl.product_id.id
                        cleaned.append(v)
                # If no pr_line_id either, skip — genuinely empty row.
            else:
                cleaned.append(v)
        if not cleaned:
            return self.browse()
        return super().create(cleaned)
