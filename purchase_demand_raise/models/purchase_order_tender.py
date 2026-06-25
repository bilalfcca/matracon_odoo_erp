"""Native Odoo alternative RFQs → Matracon comparative statement PDF."""

from collections import OrderedDict

from odoo import models, fields, api, _
from odoo.exceptions import UserError


class PurchaseOrderTender(models.Model):
    _inherit = 'purchase.order'

    # ── Per-vendor quotation terms (general — any product category) ───────────
    x_quote_brand = fields.Char(
        string='Brand / Make',
        help='Manufacturer or brand offered by this vendor (if applicable).')
    x_quote_delivery_basis = fields.Char(
        string='Delivery Basis', help='e.g. Ex-Works, FOR Site, DDP')
    x_quote_delivery_period = fields.Char(string='Delivery / Lead Time')
    x_quote_payment_terms = fields.Char(string='Payment Terms')
    x_quote_warranty = fields.Char(string='Warranty / Defect Liability')
    x_quote_tax_ids = fields.Many2many(
        'account.tax',
        'purchase_order_quote_tax_rel',
        'order_id', 'tax_id',
        string='GST / Purchase Taxes',
        domain="[('type_tax_use', '=', 'purchase')]",
        help='Selected taxes are applied to all product lines on this RFQ.',
    )
    x_quote_tax_treatment = fields.Char(
        string='Tax Notes',
        help='Optional note for CS PDF, e.g. "18% charged extra" or "inclusive". '
             'Auto-filled when taxes are selected above.',
    )
    x_quote_validity = fields.Date(string='Quote Valid Until')
    x_quote_additional_terms = fields.Html(
        string='Additional Terms & Conditions',
        sanitize_attributes=False,
        help='Free-form vendor terms: scope inclusions/exclusions, testing, '
             'mobilization, HSE, LD clauses, or any product-specific notes.',
    )

    x_is_alternative_rfq = fields.Boolean(
        string='Alternative RFQ',
        default=False,
        copy=False,
        help='True for vendor alternative quotations linked to a PR tender group. '
             'These are independent RFQs after initial data copy from the root PR.',
    )

    x_has_tender_alternatives = fields.Boolean(
        compute='_compute_x_has_tender_alternatives', store=False)

    @api.depends('purchase_group_id', 'alternative_po_ids')
    def _compute_x_has_tender_alternatives(self):
        for order in self:
            _root, orders = order._get_tender_purchase_orders()
            order.x_has_tender_alternatives = len(orders) > 1

    def _apply_quote_taxes_to_lines(self):
        """Push QS-tab taxes onto all product lines so totals recalculate."""
        for order in self:
            product_lines = order.order_line.filtered(
                lambda l: not l.display_type and l.product_id)
            if not product_lines:
                continue
            if order.x_quote_tax_ids:
                product_lines.tax_ids = order.x_quote_tax_ids

    def _onchange_x_quote_tax_ids(self):
        self._apply_quote_taxes_to_lines()
        if self.x_quote_tax_ids:
            self.x_quote_tax_treatment = ', '.join(self.x_quote_tax_ids.mapped('name'))

    def write(self, vals):
        if self.env.context.get('matracon_skip_alt_sync'):
            res = super().write(vals)
            if 'x_quote_tax_ids' in vals:
                self._apply_quote_taxes_to_lines()
            return res

        res = super().write(vals)
        if 'x_quote_tax_ids' in vals:
            self._apply_quote_taxes_to_lines()
        if vals.get('state') == 'cancel':
            for order in self.filtered(
                lambda o: o.state == 'cancel' and o.x_pr_state != 'cancelled'):
                order.x_pr_state = 'cancelled'
        if 'purchase_group_id' in vals:
            self.with_context(matracon_skip_alt_sync=True)._matracon_init_alternatives_from_root()
        return res

    @api.model_create_multi
    def create(self, vals_list):
        orders = super().create(vals_list)
        orders.with_context(matracon_skip_alt_sync=True)._matracon_init_alternatives_from_root()
        return orders

    def action_print_rfq_no_price(self):
        """Print RFQ without prices (Matracon internal / vendor quote request)."""
        self.ensure_one()
        return self.env.ref(
            'purchase_demand_raise.action_report_rfq_demand_raise'
        ).report_action(self)

    def action_confirm_after_approval(self):
        """Confirm PO immediately when HO + CEO have approved (po_locked).

        Also handles the edge case where state is already 'purchase' but
        pickings were never created (e.g., Odoo double-approval intercepted the
        original button_confirm call and no receipt was generated).
        """
        for order in self:
            if order.x_is_alternative_rfq:
                raise UserError(_(
                    'Use standard Confirm Order on alternative vendor RFQs. '
                    'This action is for the main PR document only.'
                ))
            if order.x_pr_state != 'po_locked':
                raise UserError(_('PO must be in PO Locked state to confirm.'))

            if order.state in ('purchase', 'done'):
                # Already confirmed — ensure receipt picking exists (re-trigger if missing)
                if order.state == 'purchase' and not order.picking_ids:
                    if hasattr(order, '_create_picking'):
                        order._create_picking()
                continue

            order.button_confirm()
            # Bypass native purchase double-approval — CEO approval is our final gate
            if order.state not in ('purchase', 'done', 'cancel'):
                if hasattr(order, 'button_approve'):
                    order.sudo().button_approve()
                elif hasattr(order, '_create_picking'):
                    order.write({'state': 'purchase'})
                    order._create_picking()
        return True

    # ─────────────────────────────────────────────────────────────────────────
    # TENDER / ALTERNATIVES
    # ─────────────────────────────────────────────────────────────────────────

    def _get_tender_root_pr(self):
        """Return the single PR document that owns this tender group."""
        self.ensure_one()
        if getattr(self, 'purchase_group_id', False) and self.purchase_group_id:
            pr_docs = self.purchase_group_id.order_ids.filtered(
                lambda o: o.x_is_pr_document and not o.x_is_alternative_rfq
            )
            if pr_docs:
                return pr_docs.sorted(key=lambda o: o.id)[0]
        if self.x_is_pr_document and not self.x_is_alternative_rfq:
            return self
        return self.browse()

    def _get_tender_purchase_orders(self):
        """All RFQs in the same native Odoo alternatives group."""
        self.ensure_one()
        root = self._get_tender_root_pr() or self
        if getattr(self, 'purchase_group_id', False) and self.purchase_group_id:
            orders = self.purchase_group_id.order_ids
        elif getattr(self, 'alternative_po_ids', False) and self.alternative_po_ids:
            orders = self | self.alternative_po_ids
        else:
            orders = self

        orders = orders.filtered(lambda o: o.state != 'cancel')
        return root, orders.sorted(key=lambda o: (o.partner_id.name or '', o.id))

    def _get_comparative_statement_data(self):
        """Build grid data for the comparative statement PDF from native alternatives."""
        self.ensure_one()
        root, orders = self._get_tender_purchase_orders()
        currency = root.currency_id or self.env.company.currency_id

        vendors = []
        for order in orders:
            vendors.append({
                'id': order.id,
                'name': order.partner_id.name or order.name,
                'reference': order.partner_ref or order.name,
                'order': order,
            })

        # Union of products across all alternative RFQs
        product_map = OrderedDict()
        for order in orders:
            for line in order.order_line.filtered(lambda l: not l.display_type):
                if line.product_id.id not in product_map:
                    product_map[line.product_id.id] = line.product_id

        line_rows = []
        for seq, product in enumerate(product_map.values(), start=1):
            cells = []
            for vendor in vendors:
                order = vendor['order']
                line = order.order_line.filtered(
                    lambda l, p=product: l.product_id == p and not l.display_type
                )[:1]
                if line:
                    qty = (
                        line.x_recommended_qty
                        or line.x_requested_qty
                        or line.product_qty
                    )
                    cells.append({
                        'quoted': True,
                        'qty': qty,
                        'uom': line.product_uom_id.name,
                        'unit_price': line.price_unit,
                        'subtotal': line.price_subtotal,
                        'remarks': '',
                    })
                else:
                    cells.append({
                        'quoted': False,
                        'qty': 0.0,
                        'uom': product.uom_id.name,
                        'unit_price': 0.0,
                        'subtotal': 0.0,
                        'remarks': _('Not Quoted'),
                    })
            line_rows.append({
                'seq': seq,
                'product': product,
                'description': product.display_name,
                'cells': cells,
            })

        vendor_totals = []
        lowest_id = False
        lowest_total = None
        for vendor in vendors:
            order = vendor['order']
            untaxed = order.amount_untaxed
            tax = order.amount_tax
            total = order.amount_total
            gst_rate = (tax / untaxed * 100.0) if untaxed else 0.0
            vendor_totals.append({
                'vendor_id': vendor['id'],
                'untaxed': untaxed,
                'tax': tax,
                'total': total,
                'gst_rate': gst_rate,
            })
            if lowest_total is None or total < lowest_total:
                lowest_total = total
                lowest_id = vendor['id']

        max_total = max((v['total'] for v in vendor_totals), default=0.0)
        saving = max_total - lowest_total if lowest_total is not None else 0.0

        terms_rows = [
            ('Brand / Make', 'x_quote_brand'),
            ('Delivery Basis', 'x_quote_delivery_basis'),
            ('Delivery / Lead Time', 'x_quote_delivery_period'),
            ('Payment Terms', 'x_quote_payment_terms'),
            ('Warranty / DLP', 'x_quote_warranty'),
            ('GST / Tax', 'x_quote_tax_treatment'),
            ('Quotation Ref.', 'partner_ref'),
            ('Quote Valid Until', 'x_quote_validity'),
        ]
        terms_table = []
        for label, field_name in terms_rows:
            row = {'label': label, 'values': []}
            for vendor in vendors:
                order = vendor['order']
                if field_name == 'partner_ref':
                    val = order.partner_ref or '—'
                elif field_name == 'x_quote_validity':
                    val = (
                        order.x_quote_validity.strftime('%d-%b-%Y')
                        if order.x_quote_validity else '—'
                    )
                elif field_name == 'x_quote_tax_treatment':
                    val = (
                        order.x_quote_tax_treatment
                        or ', '.join(order.x_quote_tax_ids.mapped('name'))
                        or '—'
                    )
                else:
                    val = getattr(order, field_name, False) or '—'
                row['values'].append(val)
            terms_table.append(row)

        vendor_additional_terms = [
            {
                'name': vendor['name'],
                'html': vendor['order'].x_quote_additional_terms or '',
            }
            for vendor in vendors
        ]

        project_name = (
            root.x_project_analytic_account_id.name
            if root.x_project_analytic_account_id else root.company_id.name
        )

        return {
            'root': root,
            'vendors': vendors,
            'line_rows': line_rows,
            'vendor_totals': vendor_totals,
            'lowest_vendor_id': lowest_id,
            'saving_vs_highest': saving,
            'lowest_total': lowest_total or 0.0,
            'currency': currency,
            'project_name': project_name,
            'report_date': fields.Date.today(),
            'terms_table': terms_table,
            'vendor_additional_terms': vendor_additional_terms,
            'has_additional_terms': any(v['html'] for v in vendor_additional_terms),
            'vendor_count': len(vendors),
        }

    def action_print_comparative_statement(self):
        """Print PDF grid built from native Odoo alternative RFQs."""
        self.ensure_one()
        _root, orders = self._get_tender_purchase_orders()
        if len(orders) < 2:
            raise UserError(_(
                'Create at least two vendor quotations using Odoo\'s native '
                '**Alternatives** tab (Create Alternative / Link to Existing RFQ), '
                'then print the comparative statement.'
            ))
        return self.env.ref(
            'purchase_demand_raise.action_report_comparative_statement'
        ).report_action(self)

    def action_print_comparative_statement_preview(self):
        """Open HTML preview of comparative statement (same data as PDF)."""
        self.ensure_one()
        return self.env.ref(
            'purchase_demand_raise.action_report_comparative_statement'
        ).report_action(self, config=False)

    def _sync_alternative_lines_from_root(self, root):
        """One-time copy of static demand qty fields from root PR lines."""
        self.ensure_one()
        for root_line in root.order_line.filtered(lambda l: not l.display_type):
            alt_line = self.order_line.filtered(
                lambda l, p=root_line.product_id: l.product_id == p and not l.display_type
            )[:1]
            if not alt_line:
                continue
            qty = root_line.x_requested_qty or root_line.product_qty
            alt_line.with_context(matracon_skip_alt_sync=True).write({
                'x_requested_qty': qty,
                'x_recommended_qty': root_line.x_recommended_qty or qty,
                'product_qty': root_line.x_recommended_qty or qty,
                'date_planned': root_line.date_planned,
                'analytic_distribution': root_line.analytic_distribution,
            })

    def _copy_static_fields_from_root_pr(self, root):
        """One-time copy when an alternative RFQ is linked to a PR tender group."""
        self.ensure_one()
        if self.id == root.id:
            return
        if self.state == 'cancel':
            if self.x_pr_state != 'cancelled':
                self.x_pr_state = 'cancelled'
            return
        vals = {
            'x_is_alternative_rfq': True,
            'x_category_id': root.x_category_id.id,
            'x_project_analytic_account_id': root.x_project_analytic_account_id.id,
            'x_initiator_id': root.x_initiator_id.id,
            'picking_type_id': root.picking_type_id.id,
            'date_planned': root.date_planned,
            # Copy PM signed file from root PR — visible as read-only on the alternative
            'x_pm_signed_pr': root.x_pm_signed_pr,
            'x_pm_signed_pr_filename': root.x_pm_signed_pr_filename,
            # x_pr_state / x_ho_status / x_ceo_status are intentionally NOT copied:
            # alternatives are vendor quotations, not PRs requiring separate approval.
            # Their native Odoo state (RFQ → Purchase Order) governs the vendor quote flow.
        }
        self.with_context(matracon_skip_alt_sync=True).write(vals)
        self._sync_alternative_lines_from_root(root)

    def _matracon_init_alternatives_from_root(self):
        """Copy PR context onto alternatives once when they join a tender group."""
        processed = set()
        for order in self:
            root = order._get_tender_root_pr()
            if not root or not root.x_is_pr_document:
                continue
            group_key = (
                root.purchase_group_id.id
                if getattr(root, 'purchase_group_id', False) and root.purchase_group_id
                else ('po', root.id)
            )
            if group_key in processed:
                continue
            processed.add(group_key)
            all_orders = (
                root.purchase_group_id.order_ids
                if getattr(root, 'purchase_group_id', False) and root.purchase_group_id
                else order | order.alternative_po_ids
            )
            for alt in all_orders.filtered(lambda o: o.id != root.id and not o.x_is_alternative_rfq):
                alt._copy_static_fields_from_root_pr(root)
            if getattr(order, 'purchase_group_id', False) and order.purchase_group_id:
                for cancelled in order.purchase_group_id.order_ids.filtered(
                    lambda o: o.state == 'cancel' and o.x_pr_state != 'cancelled'):
                    cancelled.x_pr_state = 'cancelled'

    @api.model
    def _matracon_upgrade_sync_alternatives(self):
        """Fix existing alternative RFQs after module upgrade."""
        groups = self.search([
            ('purchase_group_id', '!=', False),
            ('x_is_pr_document', '=', True),
            ('x_is_alternative_rfq', '=', False),
        ]).mapped('purchase_group_id')
        for group in groups:
            pr_docs = group.order_ids.filtered(
                lambda o: o.x_is_pr_document and not o.x_is_alternative_rfq
            )
            if not pr_docs:
                continue
            root = pr_docs.sorted(key=lambda o: o.id)[0]
            for alt in group.order_ids.filtered(lambda o: o.id != root.id):
                if not alt.x_is_alternative_rfq:
                    alt.with_context(matracon_skip_alt_sync=True).write({
                        'x_is_alternative_rfq': True,
                        'x_pm_signed_pr': False,
                        'x_pm_signed_pr_filename': False,
                    })
                alt._copy_static_fields_from_root_pr(root)
        for order in self.search([
            ('state', '=', 'cancel'),
            ('x_pr_state', '!=', 'cancelled'),
        ]):
            order.x_pr_state = 'cancelled'
