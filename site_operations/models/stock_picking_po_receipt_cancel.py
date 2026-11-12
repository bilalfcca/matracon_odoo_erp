"""Cancel & delete a validated Purchase Order receipt.

Odoo never lets a validated receipt be cancelled ("create a return instead"),
and a returned receipt stays linked to its PO, so the PO can never be reset to
draft. Admin, Procurement Officer and Site Store can instead undo the receipt
completely: the stock it brought in is taken back out, its returns are undone
too, the draft vendor bill / petty cash expense it created is deleted, and the
receipt is removed so the PO is free again (Received quantity goes back down).

Nothing is touched unless every check passes first:
  * the received stock is still in the warehouse (not issued / moved / reserved)
  * no posted vendor bill covers the received quantity
  * no posted petty cash expense, landed cost or fleet spare part uses it
"""

from collections import defaultdict

from markupsafe import Markup

from odoo import _, api, fields, models
from odoo.exceptions import UserError

CANCEL_RECEIPT_GROUPS = (
    'purchase_demand_raise.group_matracon_admin',
    'purchase_demand_raise.group_procurement_ho',
    'purchase_demand_raise.group_site_store',
    'base.group_system',
)


class StockPickingPoReceiptCancel(models.Model):
    _inherit = 'stock.picking'

    x_mtr_can_cancel_receipt = fields.Boolean(
        compute='_compute_x_mtr_can_cancel_receipt',
        help='Validated PO receipt that the current user may cancel and delete.')

    @api.depends_context('uid')
    @api.depends('state', 'picking_type_code', 'move_ids.purchase_line_id')
    def _compute_x_mtr_can_cancel_receipt(self):
        allowed = self._mtr_user_can_cancel_receipt()
        for picking in self:
            picking.x_mtr_can_cancel_receipt = bool(
                allowed and picking.state == 'done'
                and picking.picking_type_code == 'incoming'
                and picking.move_ids.purchase_line_id)

    def _mtr_user_can_cancel_receipt(self):
        user = self.env.user
        return any(user.has_group(g) for g in CANCEL_RECEIPT_GROUPS)

    # ─────────────────────────────────────────────────────────────────────
    # Action
    # ─────────────────────────────────────────────────────────────────────

    def action_mtr_cancel_po_receipt(self):
        if not self._mtr_user_can_cancel_receipt():
            raise UserError(_(
                'Only Matracon Admin, Procurement Officer or Site Store can '
                'cancel a received Purchase Order receipt.'))
        orders = self.env['purchase.order']
        for picking in self:
            orders |= picking.sudo()._mtr_cancel_po_receipt()
        if len(orders) == 1:
            return {
                'type': 'ir.actions.act_window',
                'res_model': 'purchase.order',
                'view_mode': 'form',
                'res_id': orders.id,
                'target': 'current',
            }
        return {'type': 'ir.actions.act_window_close'}

    def _mtr_cancel_po_receipt(self):
        """Run as sudo. Returns the purchase order(s) the receipt belonged to."""
        self.ensure_one()
        if self.picking_type_code != 'incoming' or self.state != 'done':
            raise UserError(_('Only a received (Done) receipt can be cancelled here.'))
        orders = self.move_ids.purchase_line_id.order_id
        if not orders:
            raise UserError(_('%s is not linked to a Purchase Order.', self.name))

        returns = self._mtr_receipt_returns()
        done_pickings = self | returns.filtered(lambda p: p.state == 'done')
        open_returns = returns.filtered(lambda p: p.state not in ('done', 'cancel'))
        all_pickings = self | returns
        done_moves = done_pickings.move_ids.filtered(lambda m: m.state == 'done')

        # ── 1. Checks (nothing is changed before all of them pass) ─────────
        stock_net = self._mtr_receipt_stock_net(done_moves)
        self._mtr_check_receipt_stock(stock_net)
        draft_bills = self._mtr_check_receipt_bills(all_pickings, done_moves)
        draft_expenses = self._mtr_check_receipt_other_docs(all_pickings)

        # ── 2. Take the stock back out of the warehouse ───────────────────
        Quant = self.env['stock.quant']
        for (product, location, lot, package, owner), qty in stock_net.items():
            if not product.uom_id.is_zero(qty):
                Quant._update_available_quantity(
                    product, location, -qty,
                    lot_id=lot or None, package_id=package or None, owner_id=owner or None)

        # ── 3. Accounting / valuation left by the stock moves ─────────────
        stock_entries = done_moves.account_move_id.filtered(lambda m: m.state == 'posted')
        if stock_entries:
            stock_entries._reverse_moves(
                default_values_list=[{
                    'ref': _('Cancelled receipt %s', self.name),
                    'date': fields.Date.context_today(self),
                } for _m in stock_entries],
                cancel=True)
        done_moves.account_move_id.filtered(lambda m: m.state == 'draft').unlink()
        done_moves.analytic_account_line_ids.unlink()
        if 'product.value' in self.env:
            self.env['product.value'].search([('move_id', 'in', all_pickings.move_ids.ids)]).unlink()

        # ── 4. Documents auto-created from the receipt ───────────────────
        for bill in draft_bills:
            try:
                with self.env.cr.savepoint():
                    bill.unlink()
            except Exception:
                bill.button_cancel()
        draft_expenses.unlink()

        # ── 5. Remove the receipt (and its returns) ──────────────────────
        summary = self._mtr_receipt_cancel_summary(done_moves)
        open_returns.action_cancel()
        moves = all_pickings.move_ids
        products = moves.product_id
        if moves.move_line_ids:
            # Done move lines cannot be deleted through the ORM, and their
            # stock effect was already reversed above.
            self.env['stock.move.line'].flush_model()
            self.env.cr.execute(
                'DELETE FROM stock_move_line WHERE id IN %s', [tuple(moves.move_line_ids.ids)])
            self.env['stock.move.line'].invalidate_model()
            moves.invalidate_recordset(['move_line_ids'])
        moves.write({
            'state': 'cancel',
            'purchase_line_id': False,
            'origin_returned_move_id': False,
            'move_orig_ids': [(5, 0, 0)],
            'move_dest_ids': [(5, 0, 0)],
        })
        names = ', '.join(all_pickings.mapped('name'))
        dmr = self.x_dmr_no if 'x_dmr_no' in self._fields else False
        all_pickings.unlink()

        # ── 6. Refresh costs and the PO ──────────────────────────────────
        products.filtered(lambda p: p.cost_method != 'standard')._update_standard_price()
        orders.order_line.invalidate_recordset(['qty_received'])
        orders.order_line._compute_qty_received()
        orders.invalidate_recordset(['picking_ids', 'incoming_picking_count', 'receipt_status'])
        body = Markup(_(
            'Receipt <b>%(names)s</b>%(dmr)s cancelled and deleted by <b>%(user)s</b>. '
            'The stock was taken back out and the Received quantity reduced:'
        )) % {
            'names': names,
            'dmr': Markup(' (DMR %s)') % dmr if dmr else '',
            'user': self.env.user.name,
        }
        for order in orders:
            order.message_post(body=body + summary, subtype_xmlid='mail.mt_note')
        return orders

    # ─────────────────────────────────────────────────────────────────────
    # Helpers
    # ─────────────────────────────────────────────────────────────────────

    def _mtr_receipt_returns(self):
        """All returns made from this receipt (and returns of those returns)."""
        returns = self.env['stock.picking']
        moves = self.move_ids
        while moves:
            new = moves.returned_move_ids.picking_id - returns - self
            returns |= new
            moves = new.move_ids
        return returns

    def _mtr_receipt_stock_net(self, done_moves):
        """Net quantity these moves added per internal quant key (product UoM)."""
        net = defaultdict(float)
        for ml in done_moves.move_line_ids:
            qty = ml.quantity_product_uom
            if not qty:
                continue
            if ml.location_dest_id.usage == 'internal':
                net[(ml.product_id, ml.location_dest_id, ml.lot_id,
                     ml.result_package_id, ml.owner_id)] += qty
            if ml.location_id.usage == 'internal':
                net[(ml.product_id, ml.location_id, ml.lot_id,
                     ml.package_id, ml.owner_id)] -= qty
        return net

    def _mtr_check_receipt_stock(self, stock_net):
        Quant = self.env['stock.quant']
        short = []
        for (product, location, lot, package, owner), qty in stock_net.items():
            if product.uom_id.compare(qty, 0.0) <= 0:
                continue
            available = Quant._get_available_quantity(
                product, location, lot_id=lot or None, package_id=package or None,
                owner_id=owner or None, strict=True)
            if product.uom_id.compare(available, qty) < 0:
                short.append(_(
                    '• %(product)s%(lot)s at %(loc)s: received %(qty)s, only %(avail)s '
                    'still free in stock',
                    product=product.display_name,
                    lot=(' (%s)' % lot.name) if lot else '',
                    loc=location.display_name,
                    qty=f'{qty:g}', avail=f'{max(available, 0.0):g}'))
        if short:
            raise UserError(_(
                'This receipt cannot be cancelled because part of the stock it brought in '
                'has already been issued, transferred or reserved for another transfer:\n\n'
                '%s\n\nReverse or unreserve those transfers first.', '\n'.join(short)))

    def _mtr_check_receipt_bills(self, pickings, done_moves):
        """Block on posted bills; return the draft GRN bills to delete."""
        Move = self.env['account.move']
        grn_bills = Move.search([
            ('move_type', 'in', ('in_invoice', 'in_refund')),
            ('state', '!=', 'cancel'),
            '|', ('x_stock_picking_id', 'in', pickings.ids),
            ('ref', 'in', ['GRN-%s' % n for n in pickings.mapped('name')]),
        ])
        posted = grn_bills.filtered(lambda b: b.state == 'posted')
        if posted:
            raise UserError(_(
                'This receipt cannot be cancelled because its vendor bill %s is already '
                'posted. Ask the accountant to reset it to draft (or cancel it) first.',
                ', '.join(posted.mapped('display_name'))))

        # Bills posted against the PO lines for more than will remain received.
        lines = done_moves.purchase_line_id
        removed = defaultdict(float)
        for move in done_moves.filtered('purchase_line_id'):
            qty = move.product_uom._compute_quantity(
                move.quantity, move.purchase_line_id.product_uom_id)
            removed[move.purchase_line_id] += -qty if move._is_purchase_return() else qty
        AML = self.env['account.move.line']
        over = []
        for line in lines:
            billed_lines = AML.search([
                ('purchase_line_id', '=', line.id),
                ('move_id.state', '=', 'posted'),
                ('move_id.move_type', 'in', ('in_invoice', 'in_refund')),
                ('display_type', '=', 'product'),
            ])
            billed = sum(
                (-1 if l.move_id.move_type == 'in_refund' else 1)
                * l.product_uom_id._compute_quantity(l.quantity, line.product_uom_id)
                for l in billed_lines)
            remaining = line.qty_received - removed[line]
            if line.product_uom_id.compare(billed, remaining) > 0:
                over.append(_(
                    '• %(product)s: %(billed)s billed (%(bills)s), only %(rem)s would remain received',
                    product=line.product_id.display_name, billed=f'{billed:g}',
                    bills=', '.join(billed_lines.move_id.mapped('display_name')),
                    rem=f'{remaining:g}'))
        if over:
            raise UserError(_(
                'This receipt cannot be cancelled because posted vendor bills already cover '
                'the quantity it received:\n\n%s\n\nAsk the accountant to reset or credit '
                'those bills first.', '\n'.join(over)))
        return grn_bills - posted

    def _mtr_check_receipt_other_docs(self, pickings):
        """Block on other posted documents; return draft petty cash expenses to delete."""
        expenses = self.env['x.petty.cash.expense']
        if 'x.petty.cash.expense' in self.env:
            expenses = self.env['x.petty.cash.expense'].search([('x_picking_id', 'in', pickings.ids)])
            posted = expenses.filtered(lambda e: e.state != 'draft')
            if posted:
                raise UserError(_(
                    'This receipt cannot be cancelled because petty cash expense %s made from '
                    'it is already posted. Reset that expense first.',
                    ', '.join(posted.mapped('display_name'))))
        if 'stock.landed.cost' in self.env:
            costs = self.env['stock.landed.cost'].search([
                ('picking_ids', 'in', pickings.ids), ('state', '=', 'done')])
            if costs:
                raise UserError(_(
                    'This receipt cannot be cancelled because landed cost %s is posted on it.',
                    ', '.join(costs.mapped('display_name'))))
        if 'x.fleet.spare.part' in self.env:
            parts = self.env['x.fleet.spare.part'].search([('picking_id', 'in', pickings.ids)])
            if parts:
                raise UserError(_(
                    'This receipt cannot be cancelled because fleet spare part record(s) %s '
                    'refer to it.', ', '.join(parts.mapped('display_name'))))
        return expenses

    def _mtr_receipt_cancel_summary(self, done_moves):
        rows = Markup('')
        for move in done_moves:
            sign = '−' if move._is_purchase_return() else ''
            rows += Markup('<li>%s: %s%s %s</li>') % (
                move.product_id.display_name, sign, f'{move.quantity:g}', move.product_uom.name)
        return Markup('<ul>%s</ul>') % rows if rows else Markup('')
