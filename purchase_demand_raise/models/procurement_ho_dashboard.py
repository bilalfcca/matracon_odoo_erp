"""Procurement HO dashboard — operations overview for procurement officers."""

from datetime import timedelta

from odoo import models, fields, api, _
from odoo.exceptions import UserError


class ProcurementHoDashboard(models.TransientModel):
    _name = 'x.procurement.ho.dashboard'
    _description = 'Procurement HO Dashboard'

    user_name = fields.Char(string='Officer', readonly=True)

    pending_review_count = fields.Integer(string='Pending HO Review', readonly=True)
    active_cs_count = fields.Integer(string='Active CS / RFQ', readonly=True)
    ready_dispatch_count = fields.Integer(string='Ready to Dispatch', readonly=True)
    dispatched_mtd_count = fields.Integer(string='Dispatched (MTD)', readonly=True)

    pending_review_ids = fields.Many2many(
        'purchase.order', 'proc_ho_dash_pending_rel', 'dash_id', 'order_id',
        string='Pending HO Review', readonly=True)
    active_cs_ids = fields.Many2many(
        'purchase.order', 'proc_ho_dash_cs_rel', 'dash_id', 'order_id',
        string='Active CS / RFQ', readonly=True)
    ready_dispatch_ids = fields.Many2many(
        'purchase.order', 'proc_ho_dash_dispatch_rel', 'dash_id', 'order_id',
        string='Ready to Dispatch', readonly=True)
    recent_dispatched_ids = fields.Many2many(
        'purchase.order', 'proc_ho_dash_dispatched_rel', 'dash_id', 'order_id',
        string='Recently Dispatched', readonly=True)
    activity_message_ids = fields.Many2many(
        'mail.message', 'proc_ho_dash_activity_rel', 'dash_id', 'message_id',
        string='Recent Activity', readonly=True)

    @api.model
    def _pr_base_domain(self):
        return [('x_is_pr_document', '=', True)]

    @api.model
    def _refresh_dashboard_data(self, dashboard):
        user = self.env.user
        today = fields.Date.context_today(self)
        month_start = today.replace(day=1)
        PO = self.env['purchase.order']
        base = self._pr_base_domain()

        pending_review = PO.search(
            base + [('x_pr_state', '=', 'submitted')],
            order='date_order desc',
        )
        active_cs = pending_review.filtered(lambda o: o.x_has_tender_alternatives)

        ready_dispatch = PO.search(
            base + [('x_pr_state', '=', 'po_locked')],
            order='date_order desc',
        )

        dispatched_mtd = PO.search(base + [
            ('x_pr_state', '=', 'dispatched'),
            ('write_date', '>=', fields.Datetime.to_datetime(month_start)),
        ])

        recent_dispatched = PO.search(
            base + [('x_pr_state', '=', 'dispatched')],
            order='write_date desc', limit=10,
        )

        tracked_orders = (
            pending_review | active_cs | ready_dispatch | recent_dispatched
        )
        messages = self.env['mail.message'].search([
            ('model', '=', 'purchase.order'),
            ('res_id', 'in', tracked_orders.ids),
            ('message_type', 'in', ('comment', 'notification')),
        ], order='date desc', limit=25)

        dashboard.write({
            'user_name': user.name,
            'pending_review_count': len(pending_review),
            'active_cs_count': len(active_cs),
            'ready_dispatch_count': len(ready_dispatch),
            'dispatched_mtd_count': len(dispatched_mtd),
            'pending_review_ids': [(6, 0, pending_review.ids)],
            'active_cs_ids': [(6, 0, active_cs.ids)],
            'ready_dispatch_ids': [(6, 0, ready_dispatch.ids)],
            'recent_dispatched_ids': [(6, 0, recent_dispatched.ids)],
            'activity_message_ids': [(6, 0, messages.ids)],
        })

    @api.model
    def action_open_dashboard(self):
        """Open or refresh the procurement HO dashboard."""
        if not self.env.user._matracon_is_procurement_officer():
            raise UserError(_('Only Procurement Officer (PO) users can open this dashboard.'))
        dashboard = self.create({})
        self._refresh_dashboard_data(dashboard)
        return {
            'type': 'ir.actions.act_window',
            'name': _('Procurement HO Dashboard'),
            'res_model': self._name,
            'res_id': dashboard.id,
            'view_mode': 'form',
            'target': 'current',
        }

    def action_refresh(self):
        self.ensure_one()
        self._refresh_dashboard_data(self)
        return {
            'type': 'ir.actions.act_window',
            'res_model': self._name,
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'current',
        }

    def _action_open_pos(self, domain, name):
        return {
            'type': 'ir.actions.act_window',
            'name': name,
            'res_model': 'purchase.order',
            'view_mode': 'list,form',
            'domain': self._pr_base_domain() + domain,
            'context': {'default_x_is_pr_document': True},
        }

    def action_open_pending_review(self):
        return self._action_open_pos(
            [('x_pr_state', '=', 'submitted')],
            _('Pending HO Review'),
        )

    def action_open_active_cs(self):
        pending = self.pending_review_ids.filtered('x_has_tender_alternatives')
        return {
            'type': 'ir.actions.act_window',
            'name': _('Active CS / RFQ'),
            'res_model': 'purchase.order',
            'view_mode': 'list,form',
            'domain': [('id', 'in', pending.ids)],
        }

    def action_open_ready_dispatch(self):
        return self._action_open_pos(
            [('x_pr_state', '=', 'po_locked')],
            _('Ready to Dispatch'),
        )

    def action_open_dispatched(self):
        return self._action_open_pos(
            [('x_pr_state', '=', 'dispatched')],
            _('Dispatched POs'),
        )
