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
        domain="[('category_id.name', 'in', ['Vendor', 'Subcontractor'])]",
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
        ('critical', 'Critical Site Demands'),
    ], string='Focus', default='all')

    # ── KPI tiles ─────────────────────────────────────────────────────────────
    pending_review_count = fields.Integer(string='Pending Site Demands', readonly=True)
    active_cs_count = fields.Integer(string='Active CS', readonly=True)
    ceo_pending_count = fields.Integer(string='Pending CEO Approval', readonly=True)
    confirmed_orders_count = fields.Integer(string='Confirmed Orders', readonly=True)
    pending_review_ids = fields.Many2many(
        'purchase.order', 'proc_ho_dash_pending_rel', 'dash_id', 'order_id',
        string='Pending HO Review', readonly=True)
    ceo_pending_ids = fields.Many2many(
        'purchase.order', 'proc_ho_dash_ceo_rel', 'dash_id', 'order_id',
        string='Pending CEO Approval', readonly=True)
    active_cs_ids = fields.Many2many(
        'purchase.order', 'proc_ho_dash_cs_rel', 'dash_id', 'order_id',
        string='Active CS / RFQ', readonly=True)
    critical_demand_ids = fields.Many2many(
        'purchase.order', 'proc_ho_dash_critical_rel', 'dash_id', 'order_id',
        string='Critical Site Demands', readonly=True)
    recent_po_ids = fields.Many2many(
        'purchase.order', 'proc_ho_dash_recent_po_rel', 'dash_id', 'order_id',
        string='Recent Purchase Orders', readonly=True)
    activity_message_ids = fields.Many2many(
        'mail.message', 'proc_ho_dash_activity_rel', 'dash_id', 'message_id',
        string='Recent Activity', readonly=True)

    # ─────────────────────────────────────────────────────────────────────────

    @api.model
    def _pr_base_domain(self):
        return [('x_is_pr_document', '=', True)]

    def _filter_domain(self):
        """Build domain from current filter field values (works in onchange context)."""
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

    def _compute_kpi_data(self):
        """Compute all KPI data from current filter state.

        Returns a dict of plain scalars and recordsets — usable both for
        writing to DB (via _refresh_dashboard_data) and for setting directly
        on ``self`` during an onchange (no DB writes needed).
        """
        self.ensure_one()
        user = self.env.user
        today = fields.Date.context_today(self)
        month_start = today.replace(day=1)
        PO = self.env['purchase.order']
        base = self._filter_domain()

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
            base + [('x_pr_state', '=', 'po_locked'),
                    ('state', 'in', ('purchase', 'done'))],
            order='date_order desc', limit=20,
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
        tracked = (
            pending_review | ceo_pending | active_cs | confirmed | critical | recent_pos
        )
        messages = self.env['mail.message'].search([
            ('model', '=', 'purchase.order'),
            ('res_id', 'in', tracked.ids),
            ('message_type', 'in', ('comment', 'notification')),
        ], order='date desc', limit=25)

        site_projects = self.env['x.project.site.config'].search([]).mapped(
            'analytic_account_id')

        return {
            'name': _('Procurement Operations Overview'),
            'user_name': user.name,
            # Scalar KPIs
            'pending_review_count': len(pending_review),
            'active_cs_count': len(active_cs),
            'ceo_pending_count': len(ceo_pending),
            'confirmed_orders_count': len(confirmed),
            # Recordsets (M2M)
            'available_project_ids': site_projects,
            'pending_review_ids': pending_review,
            'ceo_pending_ids': ceo_pending,
            'active_cs_ids': active_cs,
            'critical_demand_ids': critical,
            'recent_po_ids': recent_pos,
            'activity_message_ids': messages,
        }

    @api.model
    def _refresh_dashboard_data(self, dashboard):
        """Compute KPI data and persist it to the dashboard record."""
        data = dashboard._compute_kpi_data()
        write_vals = {}
        for fname, val in data.items():
            # Recordsets → M2M command; scalars pass through unchanged
            if hasattr(val, '_name'):
                write_vals[fname] = [(6, 0, val.ids)]
            else:
                write_vals[fname] = val
        # Context guard prevents write() override from triggering another refresh
        dashboard.with_context(_refreshing_dashboard=True).write(write_vals)

    # ── Filter fields that trigger a KPI refresh when written ─────────────────
    _FILTER_FIELDS = frozenset({
        'filter_project_id', 'filter_contact_id', 'filter_category_id',
        'filter_pr_state', 'filter_period_days', 'filter_section',
    })

    def write(self, vals):
        """Persist KPI data whenever filter fields are explicitly saved."""
        res = super().write(vals)
        if not self.env.context.get('_refreshing_dashboard') and (
            vals.keys() & self._FILTER_FIELDS
        ):
            for rec in self:
                self._refresh_dashboard_data(rec)
        return res

    @api.onchange(
        'filter_project_id', 'filter_contact_id', 'filter_category_id',
        'filter_pr_state', 'filter_period_days', 'filter_section',
    )
    def _onchange_filters(self):
        """Real-time filter: compute KPI data in-memory and push to the form immediately.

        Odoo rolls back any DB writes made inside an onchange handler, so we
        instead set all results directly on ``self``.  Odoo's onchange framework
        serialises these changes and returns them to the frontend, updating all
        cards and embedded lists without a page reload.
        """
        data = self._compute_kpi_data()
        for fname, val in data.items():
            setattr(self, fname, val)

    def action_clear_filters(self):
        """Reset all filters to defaults and reload the dashboard."""
        self.ensure_one()
        self.write({
            'filter_project_id': False,
            'filter_contact_id': False,
            'filter_category_id': False,
            'filter_pr_state': '',
            'filter_period_days': '30',
            'filter_section': 'all',
        })
        return {
            'type': 'ir.actions.act_window',
            'res_model': self._name,
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'current',
        }

    @api.model
    def action_open_dashboard(self):
        """Open or refresh the procurement HO dashboard."""
        user = self.env.user
        if not user._matracon_can_open_procurement_dashboard():
            raise UserError(_(
                'Only Procurement Officers or users who have raised purchase '
                'requisitions can open this dashboard.'
            ))
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

    def action_open_critical_demands(self):
        return self._action_open_pos(
            [('x_pr_state', '=', 'submitted'), ('x_ho_status', '=', 'pending')],
            _('Critical Site Demands'),
        )
