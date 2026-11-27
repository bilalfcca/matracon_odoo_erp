"""Force Complete PO: close a PO whose remaining quantity will not be delivered.

* Cancels ONLY the PO's receipts/backorders that are not yet received.
  Validated receipts, received quantities, stock, valuation and bills are
  never touched — nothing is "received", so no fake receipt or journal entry.
* Locks the PO and marks it Force Completed (by / on / reason); its delivery
  status becomes Closed and it leaves every pending-delivery list.
* Only the received quantity remains billable (no bill expected for goods
  that were never delivered, even on 'ordered quantity' billing).
* Admin can Reopen: unlocks the PO and creates a new receipt for exactly the
  quantity not yet delivered.

Who: Procurement HO and Admin for any PO; Site Store keepers for their own
site's Site Procurement POs. Reopen: Admin only.
"""
from markupsafe import Markup, escape

from odoo import api, fields, models, _
from odoo.exceptions import UserError


class PurchaseOrderForceComplete(models.Model):
    _inherit = 'purchase.order'

    x_force_completed = fields.Boolean(string='Force Completed', readonly=True, copy=False,
                                       tracking=True, index=True)
    x_force_completed_by_id = fields.Many2one('res.users', string='Force Completed By',
                                              readonly=True, copy=False)
    x_force_completed_on = fields.Datetime(string='Force Completed On', readonly=True, copy=False)
    x_force_complete_reason = fields.Text(string='Force Complete Reason', readonly=True, copy=False)
    x_can_force_complete = fields.Boolean(compute='_compute_can_force_complete')
    x_can_reopen = fields.Boolean(compute='_compute_can_force_complete')

    def _mtr_user_may_force_complete(self, user=None):
        self.ensure_one()
        user = user or self.env.user
        if user._matracon_is_admin() or user._matracon_is_procurement_officer():
            return True
        return bool(
            user.has_group('purchase_demand_raise.group_site_store')
            and self.x_procurement_type == 'site_procurement'
            and self.x_project_analytic_account_id
            and self.x_project_analytic_account_id == user.x_default_analytic_account_id
        )

    def _mtr_pending_receipts(self):
        self.ensure_one()
        return self.sudo().picking_ids.filtered(
            lambda p: p.picking_type_code == 'incoming' and p.state not in ('done', 'cancel'))

    @api.depends_context('uid')
    @api.depends('state', 'x_force_completed', 'picking_ids.state')
    def _compute_can_force_complete(self):
        is_admin = self.env.user._matracon_is_admin()
        for order in self:
            order.x_can_force_complete = bool(
                order.state == 'purchase' and not order.x_force_completed
                and order._mtr_pending_receipts() and order._mtr_user_may_force_complete())
            order.x_can_reopen = bool(order.x_force_completed and is_admin)

    # ── Actions ──────────────────────────────────────────────────────────────
    def action_open_force_complete(self):
        orders = self or self.browse(self.env.context.get('active_ids', []))
        return {
            'type': 'ir.actions.act_window',
            'name': _('Force Complete PO'),
            'res_model': 'x.po.force.complete.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {'default_order_ids': [(6, 0, orders.ids)]},
        }

    def _mtr_force_complete(self, reason):
        if not (reason or '').strip():
            raise UserError(_('Please give the reason for force-completing.'))
        for order in self:
            if not order._mtr_user_may_force_complete():
                raise UserError(_('You are not allowed to force-complete %s.') % order.name)
            if order.state != 'purchase':
                raise UserError(_('%s is not a confirmed purchase order.') % order.name)
            if order.x_force_completed:
                raise UserError(_('%s is already force-completed.') % order.name)
            pending = order._mtr_pending_receipts()
            if not pending:
                raise UserError(_(
                    '%s has no pending receipt — it is already fully received or '
                    'its receipts are cancelled.') % order.name)
            rows = []
            for line in order.order_line.filtered(lambda l: not l.display_type and l.product_id):
                not_delivered = max(line.product_qty - line.qty_received, 0.0)
                rows.append(Markup('<tr><td>%s</td><td style="text-align:right">%s</td>'
                                   '<td style="text-align:right">%s</td><td style="text-align:right"><b>%s</b></td></tr>') % (
                    line.product_id.display_name, '{:,.2f}'.format(line.product_qty),
                    '{:,.2f}'.format(line.qty_received), '{:,.2f}'.format(not_delivered)))
            pending_names = ', '.join(pending.mapped('name'))
            pending.with_context(mtr_force_complete=True).action_cancel()
            order.sudo().write({
                'x_force_completed': True,
                'x_force_completed_by_id': self.env.user.id,
                'x_force_completed_on': fields.Datetime.now(),
                'x_force_complete_reason': reason.strip(),
                'locked': True,
            })
            order._mtr_recompute_billing()
            order.message_post(body=Markup(_(
                '<b>PO force-completed</b> by %(user)s.<br/>Reason: %(reason)s<br/>'
                'Cancelled pending receipt(s): %(pickings)s (nothing was received; '
                'received quantities, stock and bills unchanged).'
                '<table class="table table-sm mt-2"><thead><tr><th>Product</th>'
                '<th style="text-align:right">Ordered</th><th style="text-align:right">Received</th>'
                '<th style="text-align:right">Not Delivered</th></tr></thead><tbody>%(rows)s</tbody></table>')) % {
                    'user': self.env.user.name, 'reason': escape(reason.strip()),
                    'pickings': pending_names, 'rows': Markup('').join(rows)})
        return True

    def action_reopen_force_completed(self):
        if not self.env.user._matracon_is_admin():
            raise UserError(_('Only an Administrator can reopen a force-completed PO.'))
        for order in self:
            if not order.x_force_completed:
                raise UserError(_('%s is not force-completed.') % order.name)
            order.sudo().write({'x_force_completed': False, 'locked': False})
            before = order.sudo().picking_ids
            order.sudo()._create_picking()
            new = order.sudo().picking_ids - before
            order._mtr_recompute_billing()
            order.message_post(body=Markup(_(
                '<b>Force completion reopened</b> by %(user)s. New receipt for the '
                'not-delivered quantity: %(pickings)s.')) % {
                    'user': self.env.user.name, 'pickings': ', '.join(new.mapped('name')) or _('none needed')})
        return True

    def button_unlock(self):
        """Force-completed POs must be reopened with 'Reopen PO' (which creates
        the receipt for the balance), not plainly unlocked."""
        if self.filtered('x_force_completed'):
            raise UserError(_('This PO was force-completed. Use "Reopen PO" (Administrator) '
                              'to reopen it and create a receipt for the balance.'))
        return super().button_unlock()

    def _mtr_recompute_billing(self):
        """Re-run billing quantities and delivery status after force-complete /
        reopen (their compute methods check x_force_completed)."""
        lines = self.order_line
        self.env.add_to_compute(lines._fields['qty_to_invoice'], lines)
        self.env.add_to_compute(lines._fields['qty_invoiced'], lines)
        self.env.add_to_compute(self._fields['x_fulfillment_status'], self)
        self.env.flush_all()

    # ── Delivery status: force-completed POs are always Closed ───────────────
    def _compute_fulfillment_status(self):   # no @api.depends: keep the original's
        super()._compute_fulfillment_status()
        for order in self.filtered(lambda o: o.x_force_completed and o.state == 'purchase'):
            order.x_fulfillment_status = 'closed'


class PurchaseOrderLineForceComplete(models.Model):
    _inherit = 'purchase.order.line'

    x_qty_not_delivered = fields.Float(
        string='Not Delivered', digits='Product Unit of Measure',
        compute='_compute_qty_not_delivered', store=True,
        help='Ordered quantity not received on a force-completed PO.')

    @api.depends('product_qty', 'qty_received', 'order_id.x_force_completed')
    def _compute_qty_not_delivered(self):
        for line in self:
            line.x_qty_not_delivered = (
                max(line.product_qty - line.qty_received, 0.0)
                if line.order_id.x_force_completed and not line.display_type else 0.0)

    def _compute_qty_invoiced(self):   # no @api.depends: keep the original's
        super()._compute_qty_invoiced()
        # Force-completed: only what was received is still to be billed.
        for line in self.filtered(lambda l: l.order_id.x_force_completed
                                  and l.order_id.state == 'purchase'):
            line.qty_to_invoice = line.qty_received - line.qty_invoiced


class POForceCompleteWizard(models.TransientModel):
    _name = 'x.po.force.complete.wizard'
    _description = 'Force Complete PO'

    order_ids = fields.Many2many('purchase.order', string='Purchase Orders', readonly=True)
    reason = fields.Text(string='Reason', required=True)
    summary = fields.Html(compute='_compute_summary', sanitize=False)

    @api.depends('order_ids')
    def _compute_summary(self):
        for wiz in self:
            rows = []
            for order in wiz.order_ids:
                pending = order._mtr_pending_receipts()
                rows.append(Markup('<tr><td>%s</td><td>%s</td><td>%s</td></tr>') % (
                    order.name, order.partner_id.display_name or '',
                    ', '.join(pending.mapped('name')) or _('— nothing pending —')))
            wiz.summary = Markup(
                '<table class="table table-sm"><thead><tr><th>PO</th><th>Vendor</th>'
                '<th>Pending receipts to cancel</th></tr></thead><tbody>%s</tbody></table>'
            ) % Markup('').join(rows)

    def action_confirm(self):
        self.ensure_one()
        if not self.order_ids:
            raise UserError(_('Select at least one purchase order.'))
        self.order_ids._mtr_force_complete(self.reason)
        return {'type': 'ir.actions.act_window_close'}
