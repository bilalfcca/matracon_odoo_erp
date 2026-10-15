"""Receipts — pick the Source Document (PO) from a dropdown and auto-fill the lines.

Inventory → Operations → Receipts: instead of typing a reference, the user picks
a confirmed Purchase Order that still has quantity to receive. The vendor,
operation type and one line per PO item (remaining quantity) are filled in.
Each line is linked to its PO line (purchase_line_id), exactly like the receipt
Odoo creates on PO confirmation, so validating it updates the PO's Received
quantities, receipt status and vendor-bill matching as normal.

The PO's own auto-created receipt still holds the same pending quantities, so on
save the pending demand elsewhere is trimmed — the PO can never be received
twice over.
"""

from odoo import Command, api, fields, models


class PurchaseOrderToReceive(models.Model):
    _inherit = 'purchase.order'

    x_mtr_to_receive = fields.Boolean(
        string='Has Quantity to Receive',
        compute='_compute_x_mtr_to_receive', search='_search_x_mtr_to_receive',
        help='At least one goods line is not yet fully received.')

    def _compute_x_mtr_to_receive(self):
        for order in self:
            order.x_mtr_to_receive = any(
                line._mtr_qty_to_receive() > 0 for line in order.order_line)

    @api.model
    def _search_x_mtr_to_receive(self, operator, value):
        # Odoo 19 normalises ('x', '=', True) to ('x', 'in', [True]) before
        # calling this, so handle both forms.
        if operator in ('=', '!='):
            wanted, negate = {bool(value)}, operator == '!='
        elif operator in ('in', 'not in'):
            wanted, negate = {bool(v) for v in value}, operator == 'not in'
        else:
            raise NotImplementedError(operator)
        if wanted == {True, False} or not wanted:
            # matches everything (or nothing)
            return [] if bool(wanted) != negate else [('id', '=', False)]
        self.env['purchase.order.line'].flush_model(['product_qty', 'qty_received', 'display_type'])
        self.env.cr.execute("""
            SELECT DISTINCT pol.order_id
              FROM purchase_order_line pol
              JOIN product_product  pp ON pp.id = pol.product_id
              JOIN product_template pt ON pt.id = pp.product_tmpl_id
             WHERE pol.display_type IS NULL
               AND pt.type = 'consu'
               AND pol.product_qty - COALESCE(pol.qty_received, 0) > 0.00001
        """)
        ids = [row[0] for row in self.env.cr.fetchall()]
        positive = (True in wanted) != negate
        return [('id', 'in' if positive else 'not in', ids)]


class PurchaseOrderLineToReceive(models.Model):
    _inherit = 'purchase.order.line'

    def _mtr_qty_to_receive(self):
        """Ordered minus received, in the PO line's unit; 0 for services/notes."""
        self.ensure_one()
        if self.display_type or self.product_id.type != 'consu':
            return 0.0
        remaining = self.product_qty - self.qty_received
        return remaining if self.product_uom_id.compare(remaining, 0.0) > 0 else 0.0


class StockPickingPoReceipt(models.Model):
    _inherit = 'stock.picking'

    x_source_po_id = fields.Many2one(
        'purchase.order', string='Source Document',
        compute='_compute_x_source_po_id', store=True, readonly=False,
        copy=False, index=True,
        domain="[('state', 'in', ('purchase', 'done')), ('x_mtr_to_receive', '=', True)]",
        help='Confirmed Purchase Order being received. Only orders with quantity '
             'still to receive are listed; selecting one fills in the lines.')

    @api.depends('move_ids.purchase_line_id')
    def _compute_x_source_po_id(self):
        # Receipts created from a PO (Odoo's own or older ones) show their PO here.
        for picking in self:
            orders = picking.move_ids.purchase_line_id.order_id
            if len(orders) == 1:
                picking.x_source_po_id = orders
            elif not picking.x_source_po_id:
                picking.x_source_po_id = False

    @api.onchange('x_source_po_id')
    def _onchange_x_source_po_id(self):
        order = self.x_source_po_id
        if not order or self.state not in ('draft', False):
            return
        if self.move_ids.purchase_line_id.order_id == order:
            return  # already filled from this PO
        self.partner_id = order.partner_id
        if order.picking_type_id.code == 'incoming':
            self.picking_type_id = order.picking_type_id
        self.origin = order.name
        commands = [Command.clear()]
        for line in order.order_line:
            qty = line._mtr_qty_to_receive()
            if not qty:
                continue
            product_qty, product_uom = line.product_uom_id._adjust_uom_quantities(
                qty, line.product_id.uom_id)
            vals = line._prepare_stock_move_vals(
                self, line._get_stock_move_price_unit(), product_qty, product_uom)
            vals.pop('picking_id', None)
            commands.append(Command.create(vals))
        self.move_ids = commands

    @api.model_create_multi
    def create(self, vals_list):
        pickings = super().create(vals_list)
        pickings._mtr_link_po_lines()
        pickings._mtr_trim_po_pending_moves()
        pickings._mtr_confirm_po_receipt()
        return pickings

    def write(self, vals):
        res = super().write(vals)
        if 'move_ids' in vals or 'x_source_po_id' in vals:
            self._mtr_link_po_lines()
            self._mtr_trim_po_pending_moves()
            self._mtr_confirm_po_receipt()
        return res

    def _mtr_confirm_po_receipt(self):
        """Confirm a draft receipt made from a PO, like the one Odoo creates on
        PO confirmation: it opens at Ready with an editable "Receiving Now", and
        receiving less than the demand offers a backorder."""
        to_confirm = self.filtered(
            lambda p: p.x_source_po_id and p.state == 'draft'
            and p.picking_type_code == 'incoming' and p.move_ids)
        if to_confirm:
            to_confirm.action_confirm()

    def _mtr_link_po_lines(self):
        """Link lines to their PO line when the client did not send it.

        Normally the onchange's purchase_line_id is saved (force_save in the
        view); this covers lines added by hand for a product on the selected PO,
        so receiving them still updates the PO's Received quantity.
        """
        for picking in self.filtered(
                lambda p: p.x_source_po_id and p.state not in ('done', 'cancel')
                and p.picking_type_code == 'incoming'):
            po_lines = picking.x_source_po_id.order_line.filtered(
                lambda l: not l.display_type and l.product_id)
            for move in picking.move_ids.filtered(
                    lambda m: not m.purchase_line_id and m.state not in ('done', 'cancel')):
                candidates = po_lines.filtered(lambda l: l.product_id == move.product_id)
                line = candidates.filtered(lambda l: l._mtr_qty_to_receive() > 0)[:1] or candidates[:1]
                if line:
                    move.sudo().write({
                        'purchase_line_id': line.id,
                        'price_unit': line._get_stock_move_price_unit(),
                    })

    def _mtr_trim_po_pending_moves(self):
        """Keep pending receipt demand per PO line within what is left to receive.

        When this receipt takes quantities from a PO line, the same quantity is
        usually still waiting on the PO's own receipt. Trim (or cancel) those
        other pending moves — newest first — so ordered − received is never
        exceeded across all open receipts.
        """
        Move = self.env['stock.move'].sudo()
        for picking in self.filtered(
                lambda p: p.x_source_po_id and p.state not in ('done', 'cancel')
                and p.picking_type_code == 'incoming'):
            mine = picking.move_ids.filtered(
                lambda m: m.purchase_line_id and m.state not in ('done', 'cancel'))
            for line in mine.purchase_line_id:
                uom = line.product_uom_id
                my_qty = sum(
                    m.product_uom._compute_quantity(m.product_uom_qty, uom)
                    for m in mine if m.purchase_line_id == line)
                others = Move.search([
                    ('purchase_line_id', '=', line.id),
                    ('picking_id', '!=', picking.id),
                    ('state', 'not in', ('done', 'cancel')),
                    ('location_dest_id.usage', '!=', 'supplier'),
                ], order='id desc')
                pending = sum(
                    o.product_uom._compute_quantity(o.product_uom_qty, uom) for o in others)
                excess = my_qty + pending - (line.product_qty - line.qty_received)
                for other in others:
                    if uom.compare(excess, 0.0) <= 0:
                        break
                    other_qty = other.product_uom._compute_quantity(other.product_uom_qty, uom)
                    if uom.compare(other_qty, excess) <= 0:
                        other._action_cancel()
                        excess -= other_qty
                    else:
                        other.product_uom_qty = uom._compute_quantity(
                            other_qty - excess, other.product_uom)
                        excess = 0.0
