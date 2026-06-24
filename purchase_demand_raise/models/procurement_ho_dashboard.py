"""Procurement HO dashboard — operations overview for procurement officers."""

from datetime import timedelta

from odoo import models, fields, api, _
from odoo.exceptions import UserError


class ProcurementHoDashboard(models.TransientModel):
    _name = 'x.procurement.ho.dashboard'
    _description = 'Procurement HO Dashboard'

    name = fields.Char(string='Title', readonly=True)
    user_name = fields.Char(string='Officer', readonly=True)

    # ── Filters (single view, multiple filters) ───────────────────────────────
    filter_project_id = fields.Many2one(
        'account.analytic.account', string='Project')
    filter_contact_id = fields.Many2one(
        'res.partner', string='Vendor / Subcontractor',
        domain="[('is_company', '=', True), '|', ('supplier_rank', '>', 0), ('category_id.name', 'ilike', 'sub')]",
    )
    filter_category_id = fields.Many2one('product.category', string='Category')

    # ── Domain helpers — set during refresh ──────────────────────────────────
    available_project_ids = fields.Many2many(
        'account.analytic.account',
        'proc_ho_dash_avail_proj_rel', 'dash_id', 'project_id',
        string='Available Projects', readonly=True,
    )
    filter_pr_state = fields.Selection([
        ('', 'All Statuses'),
        ('draft', 'Draft'),
        ('submitted', 'Submitted'),
        ('ceo_final', 'Pending CEO Approval'),
        ('po_locked', 'PO Locked'),
        ('dispatched', 'Dispatched'),
        ('rejected', 'Rejected'),
        ('cancelled', 'Cancelled'),
    ], string='PR Status', default='')
    filter_period_days = fields.Selection([
        ('7', 'Last 7 Days'),
        ('30', 'Last 30 Days'),
        ('90', 'Last 90 Days'),
        ('0', 'All Time'),
    ], string='Date Range', default='30')
    filter_section = fields.Selection([
        ('all', 'All Sections'),
        ('pending_review', 'Pending HO Review'),
        ('ceo_approval', 'Pending CEO Approval'),
        ('active_cs', 'Active CS / RFQ'),
        ('ready_dispatch', 'Ready to Dispatch'),
        ('dispatched', 'Recently Dispatched'),
        ('critical', 'Critical Site Demands'),
    ], string='Focus', default='all')

    # ── KPI tiles ─────────────────────────────────────────────────────────────
    pending_review_count = fields.Integer(string='Pending Site Demands', readonly=True)
    active_cs_count = fields.Integer(string='Active CS', readonly=True)
    ceo_pending_count = fields.Integer(string='Pending CEO Approval', readonly=True)
    confirmed_orders_count = fields.Integer(string='Confirmed Orders', readonly=True)
    ready_dispatch_count = fields.Integer(string='Ready to Dispatch', readonly=True)
    dispatched_mtd_count = fields.Integer(string='Dispatched (MTD)', readonly=True)

    pending_review_ids = fields.Many2many(
        'purchase.order', 'proc_ho_dash_pending_rel', 'dash_id', 'order_id',
        string='Pending HO Review', readonly=True)
    ceo_pending_ids = fields.Many2many(
        'purchase.order', 'proc_ho_dash_ceo_rel', 'dash_id', 'order_id',
        string='Pending CEO Approval', readonly=True)
    active_cs_ids = fields.Many2many(
        'purchase.order', 'proc_ho_dash_cs_rel', 'dash_id', 'order_id',
        string='Active CS / RFQ', readonly=True)
    ready_dispatch_ids = fields.Many2many(
        'purchase.order', 'proc_ho_dash_dispatch_rel', 'dash_id', 'order_id',
        string='Ready to Dispatch', readonly=True)
    recent_dispatched_ids = fields.Many2many(
        'purchase.order', 'proc_ho_dash_dispatched_rel', 'dash_id', 'order_id',
        string='Recently Dispatched', readonly=True)
    critical_demand_ids = fields.Many2many(
        'purchase.order', 'proc_ho_dash_critical_rel', 'dash_id', 'order_id',
        string='Critical Site Demands', readonly=True)
    recent_po_ids = fields.Many2many(
        'purchase.order', 'proc_ho_dash_recent_po_rel', 'dash_id', 'order_id',
        string='Recent Purchase Orders', readonly=True)
    activity_message_ids = fields.Many2many(
        'mail.message', 'proc_ho_dash_activity_rel', 'dash_id', 'message_id',
        string='Recent Activity', readonly=True)

    @api.model
    def _pr_base_domain(self):
        return [('x_is_pr_document', '=', True)]

    def _filter_domain(self):
        """Build domain from dashboard filter fields."""
        self.ensure_one()
        domain = list(self._pr_base_domain())
        if self.filter_project_id:
            domain.append(('x_project_analytic_account_id', '=', self.filter_project_id.id))
        if self.filter_contact_id:
            domain.append(('partner_id', '=', self.filter_contact_id.id))
        if self.filter_category_id:
            domain.append(('x_category_id', '=', self.filter_category_id.id))
        if self.filter_pr_state:
            domain.append(('x_pr_state', '=', self.filter_pr_state))
        if self.filter_period_days and self.filter_period_days != '0':
            days = int(self.filter_period_days)
            cutoff = fields.Datetime.to_datetime(
                fields.Date.context_today(self) - timedelta(days=days))
            domain.append(('date_order', '>=', cutoff))
        return domain

    @api.model
    def _refresh_dashboard_data(self, dashboard):
        user = self.env.user
        today = fields.Date.context_today(self)
        month_start = today.replace(day=1)
        PO = self.env['purchase.order']
        base = dashboard._filter_domain()

        pending_review = PO.search(
            base + [('x_pr_state', '=', 'submitted')],
            order='date_order desc',
        )
        ceo_pending = PO.search(
            base + [('x_pr_state', '=', 'ceo_final')],
            order='date_order desc',
        )
        active_cs = pending_review.filtered(lambda o: o.x_has_tender_alternatives)

        ready_dispatch = PO.search(
            base + [('x_pr_state', '=', 'po_locked')],
            order='date_order desc',
        )
        confirmed = PO.search(
            base + [('x_pr_state', 'in', ('po_locked', 'dispatched')),
                    ('state', 'in', ('purchase', 'done'))],
            order='date_order desc', limit=20,
        )

        dispatched_mtd = PO.search(base + [
            ('x_pr_state', '=', 'dispatched'),
            ('write_date', '>=', fields.Datetime.to_datetime(month_start)),
        ])

        recent_dispatched = PO.search(
            base + [('x_pr_state', '=', 'dispatched')],
            order='write_date desc', limit=10,
        )

        cutoff_critical = fields.Datetime.to_datetime(today - timedelta(days=3))
        critical = PO.search(
            base + [
                ('x_pr_state', '=', 'submitted'),
                ('x_ho_status', '=', 'pending'),
                ('date_order', '<=', cutoff_critical),
            ],
            order='date_order asc', limit=10,
        )

        recent_pos = PO.search(
            base + [('state', 'in', ('purchase', 'done'))],
            order='date_order desc', limit=8,
        )

        tracked_orders = (
            pending_review | ceo_pending | active_cs | ready_dispatch
            | recent_dispatched | critical | recent_pos
        )
        messages = self.env['mail.message'].search([
            ('model', '=', 'purchase.order'),
            ('res_id', 'in', tracked_orders.ids),
            ('message_type', 'in', ('comment', 'notification')),
        ], order='date desc', limit=25)

        # Collect valid projects from Site Project Configurations only
        site_projects = self.env['x.project.site.config'].search([]).mapped(
            'x_project_analytic_account_id')

        dashboard.write({
            'name': _('Procurement Operations Overview'),
            'user_name': user.name,
            'pending_review_count': len(pending_review),
            'active_cs_count': len(active_cs),
            'ceo_pending_count': len(ceo_pending),
            'confirmed_orders_count': len(confirmed),
            'ready_dispatch_count': len(ready_dispatch),
            'dispatched_mtd_count': len(dispatched_mtd),
            'available_project_ids': [(6, 0, site_projects.ids)],
            'pending_review_ids': [(6, 0, pending_review.ids)],
            'ceo_pending_ids': [(6, 0, ceo_pending.ids)],
            'active_cs_ids': [(6, 0, active_cs.ids)],
            'ready_dispatch_ids': [(6, 0, ready_dispatch.ids)],
            'recent_dispatched_ids': [(6, 0, recent_dispatched.ids)],
            'critical_demand_ids': [(6, 0, critical.ids)],
            'recent_po_ids': [(6, 0, recent_pos.ids)],
            'activity_message_ids': [(6, 0, messages.ids)],
        })

    @api.onchange(
        'filter_project_id', 'filter_contact_id', 'filter_category_id',
        'filter_pr_state', 'filter_period_days', 'filter_section',
    )
    def _onchange_filters(self):
        if self.id:
            self._refresh_dashboard_data(self)

    @api.model
    def action_open_dashboard(self):
        """Open or refresh the procurement HO dashboard."""
        if not self.env.user._matracon_is_procurement_officer():
            raise UserError(_('Only Procurement Officer (PO) users can open this dashboard.'))
        dashboard = self.create({})
        self._refresh_dashboard_data(dashboard)
        return {
            'type': 'ir.actions.act_window',
            'name': _('Procurement Operations Overview'),
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
            'name': _('Procurement Operations Overview'),
            'res_model': self._name,
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'current',
        }

    def action_apply_filters(self):
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
            'domain': self._filter_domain() + domain,
            'context': {'default_x_is_pr_document': True},
        }

    def action_open_pending_review(self):
        return self._action_open_pos(
            [('x_pr_state', '=', 'submitted')],
            _('Pending HO Review'),
        )

    def action_open_ceo_pending(self):
        return self._action_open_pos(
            [('x_pr_state', '=', 'ceo_final')],
            _('Pending CEO Approval'),
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

    def action_open_critical_demands(self):
        return {
            'type': 'ir.actions.act_window',
            'name': _('Critical Site Demands'),
            'res_model': 'purchase.order',
            'view_mode': 'list,form',
            'domain': [('id', 'in', self.critical_demand_ids.ids)],
        }
