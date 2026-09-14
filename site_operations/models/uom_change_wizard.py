"""Wizard to change a product's Unit of Measure and convert all open quantities.

No core models are overridden.  All changes are applied via direct SQL inside
a single transaction so they are atomic — either everything converts or nothing.

Scope of conversion (only non-historical records):
  • product.template.uom_id          — the product's internal UoM
  • stock.quant                      — on-hand + reserved quantities
  • stock.move   (not done/cancel)   — demanded + processed quantities + UoM
  • stock.move.line (not done/cancel)— quantities + UoM
  • purchase.order.line (draft/sent) — ordered quantities + UoM

Records intentionally left untouched (historical audit trail):
  • Done/cancelled stock.move + stock.move.line
  • Confirmed purchase.order.line (partially received — receipt moves already created)
  • stock.valuation.layer            — monetary amounts are unaffected by unit change
  • Posted account.move.line         — accounting history is monetary, not unit-based
"""

from odoo import models, fields, api, _
from odoo.exceptions import UserError, ValidationError


class UomChangeWizard(models.TransientModel):
    _name = 'x.uom.change.wizard'
    _description = 'Product Unit of Measure (UoM) Change Wizard'

    # ── Inputs ────────────────────────────────────────────────────────────────

    product_tmpl_id = fields.Many2one(
        'product.template', string='Product', required=True,
        domain=[('type', 'in', ('consu', 'product'))],
        help='Select the product whose unit of measure you want to change.')

    old_uom_id = fields.Many2one(
        'uom.uom', string='Current Unit',
        related='product_tmpl_id.uom_id', readonly=True)

    new_uom_id = fields.Many2one(
        'uom.uom', string='New Unit', required=True,
        help='The unit you want to switch this product to.')

    conversion_factor = fields.Float(
        string='Conversion Factor', digits=(16, 6), default=1.0,
        help='How many [Current Unit] equal 1 [New Unit]?\n\n'
             'Examples:\n'
             '  • KG → Ton   : enter 1000  (1000 KG = 1 Ton)\n'
             '  • Piece → Dozen: enter 12  (12 Pieces = 1 Dozen)\n'
             '  • Meter → KM  : enter 1000 (1000 m = 1 km)\n\n'
             'Formula applied:  new_quantity = old_quantity ÷ factor')

    # ── Preview (computed, read-only) ─────────────────────────────────────────

    current_on_hand = fields.Float(
        string='Current On-Hand', readonly=True,
        compute='_compute_preview', digits=(16, 3))
    converted_on_hand = fields.Float(
        string='On-Hand After Conversion', readonly=True,
        compute='_compute_preview', digits=(16, 3))
    open_moves_count = fields.Integer(
        string='Open Stock Moves', readonly=True,
        compute='_compute_preview',
        help='Non-done, non-cancelled stock moves that will be updated.')
    open_po_lines_count = fields.Integer(
        string='Draft/Sent PO Lines', readonly=True,
        compute='_compute_preview',
        help='Purchase order lines in draft or sent state that will be updated.')

    @api.depends('product_tmpl_id', 'conversion_factor')
    def _compute_preview(self):
        for wiz in self:
            product_ids = wiz.product_tmpl_id.product_variant_ids.ids if wiz.product_tmpl_id else []
            if not product_ids:
                wiz.current_on_hand = 0.0
                wiz.converted_on_hand = 0.0
                wiz.open_moves_count = 0
                wiz.open_po_lines_count = 0
                continue

            cr = wiz.env.cr

            cr.execute(
                "SELECT COALESCE(SUM(quantity), 0) FROM stock_quant WHERE product_id = ANY(%s)",
                (product_ids,))
            total_qty = float(cr.fetchone()[0])
            wiz.current_on_hand = total_qty

            factor = wiz.conversion_factor or 1.0
            wiz.converted_on_hand = total_qty / factor

            cr.execute("""
                SELECT COUNT(*) FROM stock_move
                WHERE product_id = ANY(%s) AND state NOT IN ('done', 'cancel')
            """, (product_ids,))
            wiz.open_moves_count = cr.fetchone()[0]

            cr.execute("""
                SELECT COUNT(*) FROM purchase_order_line pol
                JOIN purchase_order po ON po.id = pol.order_id
                WHERE pol.product_id = ANY(%s) AND po.state IN ('draft', 'sent')
            """, (product_ids,))
            wiz.open_po_lines_count = cr.fetchone()[0]

    # ── Validation ────────────────────────────────────────────────────────────

    @api.constrains('conversion_factor')
    def _check_factor(self):
        for wiz in self:
            if wiz.conversion_factor <= 0:
                raise ValidationError(_('Conversion factor must be greater than zero.'))

    # ── Apply ─────────────────────────────────────────────────────────────────

    def action_apply(self):
        self.ensure_one()

        if not self.product_tmpl_id or not self.new_uom_id:
            raise UserError(_('Product and New Unit are required.'))
        if self.old_uom_id.id == self.new_uom_id.id:
            raise UserError(_(
                'New unit is the same as the current unit (%s). No change needed.'
            ) % self.old_uom_id.name)
        if self.conversion_factor <= 0:
            raise ValidationError(_('Conversion factor must be greater than zero.'))

        factor = self.conversion_factor
        old_uom_id = self.old_uom_id.id
        new_uom_id = self.new_uom_id.id
        tmpl_id = self.product_tmpl_id.id
        product_ids = self.product_tmpl_id.product_variant_ids.ids

        if not product_ids:
            raise UserError(_('No product variants found for this template.'))

        old_uom_name = self.old_uom_id.name
        new_uom_name = self.new_uom_id.name
        old_on_hand = self.current_on_hand

        cr = self.env.cr

        # ── 1. product.template — change internal UoM ─────────────────────────
        cr.execute(
            "UPDATE product_template SET uom_id = %s WHERE id = %s",
            (new_uom_id, tmpl_id))

        # ── 2. stock.quant — convert on-hand and reserved quantities ──────────
        #    stock.quant has no UoM column — always stored in product's UoM.
        cr.execute("""
            UPDATE stock_quant
            SET quantity          = quantity          / %s,
                reserved_quantity = reserved_quantity / %s
            WHERE product_id = ANY(%s)
        """, (factor, factor, product_ids))

        # ── 3. stock.move (non-done) — only where move UoM = old product UoM ──
        #    product_qty (stored computed: qty in product's internal UoM) and
        #    product_uom_qty (qty in the move's own UoM) are both converted.
        #    quantity = the "done" portion on a pending move (may be 0 or partial).
        cr.execute("""
            UPDATE stock_move
            SET product_uom_qty = product_uom_qty / %s,
                product_qty     = product_qty     / %s,
                quantity        = CASE WHEN quantity > 0 THEN quantity / %s ELSE quantity END,
                product_uom     = %s
            WHERE product_id  = ANY(%s)
              AND state       NOT IN ('done', 'cancel')
              AND product_uom = %s
        """, (factor, factor, factor, new_uom_id, product_ids, old_uom_id))

        # ── 4. stock.move.line (non-done) ─────────────────────────────────────
        cr.execute("""
            UPDATE stock_move_line
            SET quantity             = quantity             / %s,
                quantity_product_uom = quantity_product_uom / %s,
                product_uom_id       = %s
            WHERE product_id     = ANY(%s)
              AND state          NOT IN ('done', 'cancel')
              AND product_uom_id = %s
        """, (factor, factor, new_uom_id, product_ids, old_uom_id))

        # ── 5. purchase.order.line (draft/sent POs only) ──────────────────────
        #    Confirmed POs may have partial receipts in flight — leave those alone.
        cr.execute("""
            UPDATE purchase_order_line pol
            SET product_qty    = pol.product_qty / %s,
                product_uom_id = %s
            FROM purchase_order po
            WHERE po.id         = pol.order_id
              AND pol.product_id = ANY(%s)
              AND po.state       IN ('draft', 'sent')
              AND pol.product_uom_id = %s
        """, (factor, new_uom_id, product_ids, old_uom_id))

        # ── 6. Invalidate ORM cache ───────────────────────────────────────────
        self.env['product.template'].invalidate_model(['uom_id'])
        self.env['product.product'].invalidate_model()
        self.env['stock.quant'].invalidate_model()
        self.env['stock.move'].invalidate_model()
        self.env['stock.move.line'].invalidate_model()
        self.env['purchase.order.line'].invalidate_model()

        # ── 7. Log to chatter on the product ─────────────────────────────────
        self.product_tmpl_id.message_post(body=_(
            '<b>Unit of Measure changed:</b> %(old)s → %(new)s '
            '(factor: %(f)s)<br/>'
            'On-hand converted: %(oh)s %(old)s → %(noh)s %(new)s<br/>'
            'Open stock moves and draft PO lines updated accordingly.'
        ) % {
            'old': old_uom_name,
            'new': new_uom_name,
            'f': factor,
            'oh': f'{old_on_hand:,.3f}',
            'noh': f'{old_on_hand / factor:,.3f}',
        })

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('UoM Conversion Complete'),
                'message': _(
                    'Product "%(product)s" converted from %(old)s → %(new)s.\n'
                    'On-hand: %(oh)s → %(noh)s %(new)s.\n'
                    '%(moves)s open moves and %(po)s PO lines updated.'
                ) % {
                    'product': self.product_tmpl_id.name,
                    'old': old_uom_name,
                    'new': new_uom_name,
                    'oh': f'{old_on_hand:,.3f}',
                    'noh': f'{old_on_hand / factor:,.3f}',
                    'moves': self.open_moves_count,
                    'po': self.open_po_lines_count,
                },
                'type': 'success',
                'sticky': True,
            }
        }
