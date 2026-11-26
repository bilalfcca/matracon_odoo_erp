"""Vendor-wise product information, reusing existing purchase data.

* product.template.x_vendor_ids / x_last_vendor_id / x_last_purchase_date:
  vendors this product is bought from — filled automatically from confirmed
  purchase orders, plus any vendors listed in the product's Vendor Pricelists
  (planned vendors). Kept up to date on PO confirm / cancel / lock and when
  pricelists change.
* stock.move.x_vendor_id (+ stock.move.line): the PO vendor on receipts;
  the product's last vendor on issues / transfers / returns.
* x.vendor.product.report: Vendor-wise Products (SQL view over PO lines and
  Vendor Pricelists) — Inventory → Reporting.
"""
from odoo import api, fields, models, tools, _

CONFIRMED = ('purchase', 'done')


class ProductTemplateVendors(models.Model):
    _inherit = 'product.template'

    x_vendor_ids = fields.Many2many(
        'res.partner', 'x_product_tmpl_vendor_rel', 'product_tmpl_id', 'partner_id',
        string='Vendors', readonly=True, copy=False,
        help='Vendors this product is purchased from: confirmed purchase orders '
             'plus vendors listed in the Vendor Pricelists. Updated automatically.')
    x_last_vendor_id = fields.Many2one(
        'res.partner', string='Last Vendor', readonly=True, copy=False, index=True)
    x_last_purchase_date = fields.Date(string='Last Purchased', readonly=True, copy=False)

    def _mtr_refresh_vendors(self):
        """Recompute vendor fields from confirmed PO lines + Vendor Pricelists."""
        if not self:
            return
        cr = self.env.cr
        self.env.flush_all()
        cr.execute("""
            SELECT pp.product_tmpl_id, COALESCE(rp.commercial_partner_id, po.partner_id),
                   MAX(COALESCE(po.date_approve, po.date_order))
              FROM purchase_order_line pol
              JOIN purchase_order po ON po.id = pol.order_id
              JOIN product_product pp ON pp.id = pol.product_id
              LEFT JOIN res_partner rp ON rp.id = po.partner_id
             WHERE pp.product_tmpl_id IN %s
               AND po.state IN %s
               AND po.partner_id IS NOT NULL
               AND pol.display_type IS NULL
          GROUP BY pp.product_tmpl_id, COALESCE(rp.commercial_partner_id, po.partner_id)
        """, (tuple(self.ids), CONFIRMED))
        bought = {}
        for tmpl_id, partner_id, last in cr.fetchall():
            bought.setdefault(tmpl_id, []).append((last, partner_id))
        cr.execute("""
            SELECT product_tmpl_id, partner_id FROM product_supplierinfo
             WHERE product_tmpl_id IN %s AND partner_id IS NOT NULL
        """, (tuple(self.ids),))
        planned = {}
        for tmpl_id, partner_id in cr.fetchall():
            planned.setdefault(tmpl_id, set()).add(partner_id)
        for tmpl in self.sudo():
            rows = sorted(bought.get(tmpl.id, []), key=lambda r: (r[0] is not None, r[0]))
            vendor_ids = {p for _d, p in rows} | planned.get(tmpl.id, set())
            last = rows[-1] if rows else (None, None)
            vals = {
                'x_vendor_ids': [(6, 0, sorted(vendor_ids))],
                'x_last_vendor_id': last[1] or (sorted(planned.get(tmpl.id, set()))[:1] or [False])[0],
                'x_last_purchase_date': last[0].date() if last[0] else False,
            }
            tmpl.with_context(tracking_disable=True).write(vals)

    @api.model
    def _mtr_backfill_vendors(self):
        """One-time fill for all products (module upgrade)."""
        self.env.cr.execute("""
            SELECT DISTINCT pp.product_tmpl_id
              FROM purchase_order_line pol
              JOIN purchase_order po ON po.id = pol.order_id
              JOIN product_product pp ON pp.id = pol.product_id
             WHERE po.state IN %s
            UNION
            SELECT DISTINCT product_tmpl_id FROM product_supplierinfo
        """, (CONFIRMED,))
        ids = [r[0] for r in self.env.cr.fetchall() if r[0]]
        for i in range(0, len(ids), 500):
            self.browse(ids[i:i + 500])._mtr_refresh_vendors()
        return len(ids)


class ProductProductVendors(models.Model):
    _inherit = 'product.product'

    x_vendor_ids = fields.Many2many(related='product_tmpl_id.x_vendor_ids', string='Vendors')
    x_last_vendor_id = fields.Many2one(
        related='product_tmpl_id.x_last_vendor_id', store=True, string='Last Vendor', index=True)
    x_last_purchase_date = fields.Date(related='product_tmpl_id.x_last_purchase_date')


class PurchaseOrderVendors(models.Model):
    _inherit = 'purchase.order'

    def _mtr_refresh_product_vendors(self):
        tmpls = self.sudo().order_line.product_id.product_tmpl_id
        tmpls._mtr_refresh_vendors()

    def write(self, vals):
        """Any status change (confirm, CS winner, lock, cancel, reset…) refreshes
        the products' vendors — whichever path changed it."""
        res = super().write(vals)
        if 'state' in vals or 'partner_id' in vals:
            self._mtr_refresh_product_vendors()
        return res


class SupplierinfoVendors(models.Model):
    _inherit = 'product.supplierinfo'

    @api.model_create_multi
    def create(self, vals_list):
        recs = super().create(vals_list)
        recs.product_tmpl_id._mtr_refresh_vendors()
        return recs

    def write(self, vals):
        before = self.product_tmpl_id
        res = super().write(vals)
        (before | self.product_tmpl_id)._mtr_refresh_vendors()
        return res

    def unlink(self):
        tmpls = self.product_tmpl_id
        res = super().unlink()
        tmpls.exists()._mtr_refresh_vendors()
        return res


class StockMoveVendor(models.Model):
    _inherit = 'stock.move'

    x_vendor_id = fields.Many2one(
        'res.partner', string='Vendor', compute='_compute_x_vendor_id',
        store=True, index=True,
        help='Receipts from a PO: the PO vendor. Other moves: the product\'s last vendor.')

    @api.depends('purchase_line_id.order_id.partner_id', 'product_id')
    def _compute_x_vendor_id(self):
        for move in self:
            vendor = move.purchase_line_id.order_id.partner_id
            if vendor:
                vendor = vendor.commercial_partner_id
            move.x_vendor_id = vendor or move.product_id.product_tmpl_id.x_last_vendor_id


class StockMoveLineVendor(models.Model):
    _inherit = 'stock.move.line'

    x_vendor_id = fields.Many2one(related='move_id.x_vendor_id', store=True,
                                  string='Vendor', index=True)


class PurchaseOrderLineVendors(models.Model):
    _inherit = 'purchase.order.line'

    x_product_vendor_ids = fields.Many2many(
        related='product_id.x_vendor_ids', string='Product Vendors')


class VendorProductReport(models.Model):
    _name = 'x.vendor.product.report'
    _description = 'Vendor-wise Products'
    _auto = False
    _order = 'vendor_id, product_id'
    _rec_name = 'product_id'

    vendor_id = fields.Many2one('res.partner', string='Vendor', readonly=True)
    product_id = fields.Many2one('product.product', string='Product', readonly=True)
    product_tmpl_id = fields.Many2one('product.template', string='Product Template', readonly=True)
    categ_id = fields.Many2one('product.category', string='Category', readonly=True)
    uom_id = fields.Many2one('uom.uom', string='Unit', readonly=True)
    company_id = fields.Many2one('res.company', readonly=True)
    currency_id = fields.Many2one('res.currency', readonly=True)
    source = fields.Selection([
        ('purchased', 'Purchased (confirmed POs)'),
        ('planned', 'Planned (Vendor Pricelist only)'),
    ], string='Source', readonly=True)
    po_count = fields.Integer(string='POs', readonly=True, aggregator=None)  # a PO with 2 products must not count twice
    qty_ordered = fields.Float(string='Qty Ordered', readonly=True, digits='Product Unit of Measure')
    qty_received = fields.Float(string='Qty Received', readonly=True, digits='Product Unit of Measure')
    first_purchase_date = fields.Datetime(string='First Purchased', readonly=True, aggregator='min')
    last_purchase_date = fields.Datetime(string='Last Purchased', readonly=True, aggregator='max')
    last_price = fields.Monetary(string='Last Price', readonly=True, aggregator=None)
    qty_available = fields.Float(string='On Hand', compute='_compute_qty_available',
                                 digits='Product Unit of Measure')

    def _compute_qty_available(self):
        for rec in self:
            rec.qty_available = rec.product_id.qty_available

    def init(self):
        tools.drop_view_if_exists(self.env.cr, self._table)
        self.env.cr.execute(f"""
            CREATE OR REPLACE VIEW {self._table} AS (
            WITH lines AS (
                SELECT COALESCE(rp.commercial_partner_id, po.partner_id) AS vendor_id,
                       pol.product_id, po.company_id, po.currency_id, po.id AS order_id,
                       pol.product_qty, pol.qty_received, pol.price_unit,
                       COALESCE(po.date_approve, po.date_order) AS dt,
                       ROW_NUMBER() OVER (
                           PARTITION BY COALESCE(rp.commercial_partner_id, po.partner_id), pol.product_id, po.company_id
                           ORDER BY COALESCE(po.date_approve, po.date_order) DESC, pol.id DESC) AS rn
                  FROM purchase_order_line pol
                  JOIN purchase_order po ON po.id = pol.order_id
             LEFT JOIN res_partner rp ON rp.id = po.partner_id
                 WHERE po.state IN ('purchase', 'done')
                   AND po.partner_id IS NOT NULL
                   AND pol.product_id IS NOT NULL
                   AND pol.display_type IS NULL
            ), bought AS (
                SELECT vendor_id, product_id, company_id,
                       MAX(currency_id) AS currency_id,
                       COUNT(DISTINCT order_id) AS po_count,
                       SUM(product_qty) AS qty_ordered,
                       SUM(qty_received) AS qty_received,
                       MIN(dt) AS first_purchase_date,
                       MAX(dt) AS last_purchase_date,
                       MAX(CASE WHEN rn = 1 THEN price_unit END) AS last_price
                  FROM lines
              GROUP BY vendor_id, product_id, company_id
            ), planned AS (
                SELECT DISTINCT COALESCE(rp.commercial_partner_id, si.partner_id) AS vendor_id,
                       COALESCE(si.product_id, pp.id) AS product_id,
                       COALESCE(si.company_id, pt.company_id) AS company_id,
                       si.currency_id
                  FROM product_supplierinfo si
                  JOIN product_template pt ON pt.id = si.product_tmpl_id
                  JOIN product_product pp ON pp.product_tmpl_id = pt.id
             LEFT JOIN res_partner rp ON rp.id = si.partner_id
                 WHERE si.product_id IS NULL OR si.product_id = pp.id
            ), allrows AS (
                SELECT b.vendor_id, b.product_id, b.company_id, b.currency_id, 'purchased' AS source,
                       b.po_count, b.qty_ordered, b.qty_received, b.first_purchase_date,
                       b.last_purchase_date, b.last_price
                  FROM bought b
                UNION ALL
                SELECT p.vendor_id, p.product_id, p.company_id, p.currency_id, 'planned',
                       0, 0, 0, NULL, NULL, NULL
                  FROM planned p
                 WHERE NOT EXISTS (SELECT 1 FROM bought b WHERE b.vendor_id = p.vendor_id
                                     AND b.product_id = p.product_id)
            )
            SELECT ROW_NUMBER() OVER (ORDER BY a.vendor_id, a.product_id) AS id,
                   a.vendor_id, a.product_id, pp.product_tmpl_id, pt.categ_id,
                   pt.uom_id, a.company_id, a.currency_id, a.source, a.po_count,
                   a.qty_ordered, a.qty_received, a.first_purchase_date,
                   a.last_purchase_date, a.last_price
              FROM allrows a
              JOIN product_product pp ON pp.id = a.product_id
              JOIN product_template pt ON pt.id = pp.product_tmpl_id
            )
        """)

    def action_view_purchase_lines(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('%(product)s from %(vendor)s') % {
                'product': self.product_id.display_name, 'vendor': self.vendor_id.display_name},
            'res_model': 'purchase.order.line',
            'view_mode': 'list,form',
            'domain': [('product_id', '=', self.product_id.id),
                       ('order_id.partner_id', 'child_of', self.vendor_id.id),
                       ('order_id.state', 'in', CONFIRMED)],
        }
