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
        res = super().write(vals)
        if 'x_quote_tax_ids' in vals:
            self._apply_quote_taxes_to_lines()
        return res

    # ─────────────────────────────────────────────────────────────────────────
    # TENDER / ALTERNATIVES
    # ─────────────────────────────────────────────────────────────────────────

    def _get_tender_purchase_orders(self):
        """All RFQs in the same native Odoo alternatives group."""
        self.ensure_one()
        root = self
        if getattr(self, 'purchase_group_id', False) and self.purchase_group_id:
            orders = self.purchase_group_id.order_ids
            pr_docs = orders.filtered('x_is_pr_document')
            if pr_docs:
                root = pr_docs[0]
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
        """Align alternative RFQ line qty/analytic with the originating PR."""
        self.ensure_one()
        for root_line in root.order_line.filtered(lambda l: not l.display_type):
            alt_line = self.order_line.filtered(
                lambda l, p=root_line.product_id: l.product_id == p and not l.display_type
            )[:1]
            if not alt_line:
                continue
            qty = root_line.x_requested_qty or root_line.product_qty
            alt_line.write({
                'x_requested_qty': qty,
                'x_recommended_qty': root_line.x_recommended_qty or qty,
                'product_qty': qty,
                'date_planned': root_line.date_planned,
                'analytic_distribution': root_line.analytic_distribution,
            })
