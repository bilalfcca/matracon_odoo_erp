"""Site Store Manager dashboard — matches Management Dashboard mockup in site_store_flow.docx."""

from datetime import timedelta

from odoo import models, fields, api, _
from odoo.exceptions import UserError


class SiteStoreDashboard(models.TransientModel):
    _name = 'x.site.store.dashboard'
    _description = 'Site Store Manager Dashboard'

    # ── Context header ────────────────────────────────────────────────────────
    name = fields.Char(string='Title', readonly=True)
    project_analytic_account_id = fields.Many2one(
        'account.analytic.account', string='Project', readonly=True)
    project_name = fields.Char(string='Project Name', readonly=True)
    user_name = fields.Char(string='Store Manager', readonly=True)

    # ── Filters ───────────────────────────────────────────────────────────────
    filter_pr_state = fields.Selection([
        ('', 'All PR Statuses'),
        ('draft', 'Draft'),
        ('submitted', 'Submitted'),
        ('ceo_final', 'Pending CEO'),
        ('po_locked', 'PO Locked'),
        ('dispatched', 'Dispatched'),
    ], string='PR Status', default='')
    filter_period_days = fields.Selection([
        ('7', 'Last 7 Days'),
        ('30', 'Last 30 Days'),
        ('90', 'Last 90 Days'),
        ('0', 'All Time'),
    ], string='Date Range', default='30')
    filter_section = fields.Selection([
        ('all', 'All Sections'),
        ('prs', 'Requisitions'),
        ('receipts', 'Material Receipts'),
        ('issuances', 'Material Issuances'),
        ('transfers', 'Site Transfers'),
        ('returns', 'Pending Returns'),
    ], string='Focus', default='all')

    # ── Replenishment alert (critical stock) ──────────────────────────────────
    has_stock_alert = fields.Boolean(readonly=True)
    alert_product_id = fields.Many2one('product.product', readonly=True)
    alert_qty_on_hand = fields.Float(readonly=True, digits='Product Unit of Measure')
    alert_min_qty = fields.Float(readonly=True, digits='Product Unit of Measure')
    alert_message = fields.Char(readonly=True)

    # ── KPI tiles ─────────────────────────────────────────────────────────────
    open_pr_count = fields.Integer(string='Open Requisitions', readonly=True)
    pending_signature_count = fields.Integer(string='Pending PM Signature', readonly=True)
    arrivals_today_count = fields.Integer(string='Arrivals Today', readonly=True)
    partial_receipts_count = fields.Integer(string='Partial Receipts', readonly=True)
    issuance_mtd_count = fields.Integer(string='Issuances (MTD)', readonly=True)
    pending_returns_count = fields.Integer(string='Pending Returns', readonly=True)
    pending_transfer_count = fields.Integer(string='Transfers In Progress', readonly=True)
    dest_receipt_count = fields.Integer(string='Incoming Transfers', readonly=True)

    normal_issuance_pct = fields.Integer(string='Normal Issuance %', readonly=True)
    subcontractor_issuance_pct = fields.Integer(string='Subcontractor %', readonly=True)

    # ── Embedded lists ──────────────────────────────────────────────────────────
    pr_ids = fields.Many2many(
        'purchase.order', 'site_store_dash_pr_rel', 'dash_id', 'order_id',
        string='Active Requisitions', readonly=True)
    pending_receipt_ids = fields.Many2many(
        'stock.picking', 'site_store_dash_receipt_pending_rel', 'dash_id', 'picking_id',
        string='Receipts To Validate', readonly=True)
    recent_grn_ids = fields.Many2many(
        'stock.picking', 'site_store_dash_grn_recent_rel', 'dash_id', 'picking_id',
        string='Recent GRNs', readonly=True)
    on_order_po_ids = fields.Many2many(
        'purchase.order', 'site_store_dash_on_order_rel', 'dash_id', 'order_id',
        string='On Order', readonly=True)
    transfer_ids = fields.Many2many(
        'stock.picking', 'site_store_dash_transfer_rel', 'dash_id', 'picking_id',
        string='Site Transfers', readonly=True)
    recent_issuance_ids = fields.Many2many(
        'stock.picking', 'site_store_dash_issuance_rel', 'dash_id', 'picking_id',
        string='Recent Issuances', readonly=True)
    pending_return_ids = fields.Many2many(
        'stock.picking', 'site_store_dash_return_rel', 'dash_id', 'picking_id',
        string='Pending Returns', readonly=True)

    # ─────────────────────────────────────────────────────────────────────────

    @api.model
    def _project_analytic(self):
        return self.env.user.x_default_analytic_account_id

    @api.model
    def _project_domain_po(self, analytic):
        if not analytic:
            return [('id', '=', 0)]
        return [
            ('x_project_analytic_account_id', '=', analytic.id),
            ('x_is_pr_document', '=', True),
        ]

    def _po_filter_domain(self, analytic):
        domain = self._project_domain_po(analytic)
        if self.filter_pr_state:
            domain.append(('x_pr_state', '=', self.filter_pr_state))
        if self.filter_period_days and self.filter_period_days != '0':
            days = int(self.filter_period_days)
            cutoff = fields.Datetime.to_datetime(
                fields.Date.context_today(self) - timedelta(days=days))
            domain.append(('date_order', '>=', cutoff))
        return domain

    # ── Filter fields that trigger a KPI refresh when written ─────────────────
    _FILTER_FIELDS = frozenset({
        'filter_pr_state', 'filter_period_days', 'filter_section',
    })

    def write(self, vals):
        """Auto-refresh KPI data whenever filter fields are saved."""
        res = super().write(vals)
        if not self.env.context.get('_refreshing_dashboard') and (
            vals.keys() & self._FILTER_FIELDS
        ):
            for rec in self:
                self._refresh_dashboard_data(rec)
        return res

    @api.onchange('filter_pr_state', 'filter_period_days', 'filter_section')
    def _onchange_filters(self):
        """Live filter: write filter values to DB so write() override picks them up."""
        if not self.id:
            return
        new_vals = {
            'filter_pr_state': self.filter_pr_state or '',
            'filter_period_days': self.filter_period_days or '30',
            'filter_section': self.filter_section or 'all',
        }
        self.browse(self.id).write(new_vals)
        # Push updated scalar counts back to the form for live display
        fresh = self.sudo().browse(self.id).read([
            'open_pr_count', 'pending_signature_count', 'arrivals_today_count',
            'partial_receipts_count', 'issuance_mtd_count', 'pending_returns_count',
            'pending_transfer_count', 'normal_issuance_pct', 'subcontractor_issuance_pct',
        ])[0]
        for fname, val in fresh.items():
            if fname != 'id':
                setattr(self, fname, val)

    def action_clear_filters(self):
        """Reset all filters to defaults and reload the dashboard."""
        self.ensure_one()
        self.write({
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
    def _refresh_dashboard_data(self, dashboard):
        user = self.env.user
        analytic = user.x_default_analytic_account_id
        warehouse = user.x_default_warehouse_id
        today = fields.Date.context_today(self)
        month_start = today.replace(day=1)

        PO = self.env['purchase.order']
        Picking = self.env['stock.picking']
        Orderpoint = self.env['stock.warehouse.orderpoint']

        po_base = dashboard._po_filter_domain(analytic)

        # ── KPI: PRs ────────────────────────────────────────────────────────
        open_prs = PO.search(po_base + [
            ('x_pr_state', 'in', ('draft', 'submitted', 'ceo_final', 'po_locked')),
        ])
        pending_sig = PO.search(po_base + [
            ('x_pr_state', '=', 'draft'),
            ('x_pm_signed_pr', '=', False),
        ])

        # ── KPI: Receipts ───────────────────────────────────────────────────
        receipt_domain = [
            ('picking_type_code', '=', 'incoming'),
            ('state', 'not in', ('done', 'cancel')),
        ]
        if warehouse:
            receipt_domain.append(('picking_type_id.warehouse_id', '=', warehouse.id))
        if analytic:
            receipt_domain.append(
                ('purchase_id.x_project_analytic_account_id', '=', analytic.id))
        pending_receipts = Picking.search(receipt_domain, limit=20)

        arrivals_today = Picking.search(receipt_domain + [
            ('scheduled_date', '>=', fields.Datetime.to_datetime(today)),
            ('scheduled_date', '<',
             fields.Datetime.to_datetime(today + timedelta(days=1))),
        ])
        partial_receipts = pending_receipts.filtered(
            lambda p: any(
                m.product_uom_qty > m.quantity for m in p.move_ids if m.product_id
            )
        )

        recent_grns = Picking.search([
            ('picking_type_code', '=', 'incoming'),
            ('state', '=', 'done'),
            ('purchase_id.x_project_analytic_account_id', '=', analytic.id if analytic else 0),
        ], order='date_done desc', limit=8)

        on_order = PO.search(po_base + [
            ('x_pr_state', 'in', ('po_locked', 'dispatched')),
            ('state', 'in', ('purchase', 'done')),
        ], order='date_order desc', limit=8)

        # ── KPI: Issuances (no amounts — site store must not see prices) ────
        iss_domain = [
            ('x_transfer_purpose', '=', 'material_issuance'),
            ('x_is_return_transfer', '=', False),
        ]
        if analytic:
            iss_domain.append(('x_issuance_project_id', '=', analytic.id))
        issuances_mtd = Picking.search(iss_domain + [
            ('state', '=', 'done'),
            ('date_done', '>=', fields.Datetime.to_datetime(month_start)),
        ])
        recent_issuances = Picking.search(iss_domain, order='id desc', limit=8)

        normal_count = len(issuances_mtd.filtered(lambda p: p.x_issue_type == 'normal'))
        sub_count = len(issuances_mtd.filtered(lambda p: p.x_issue_type == 'subcontractor'))
        total_iss = normal_count + sub_count
        normal_pct = int(round(normal_count * 100 / total_iss)) if total_iss else 0
        sub_pct = 100 - normal_pct if total_iss else 0

        # ── Returns awaiting validation ─────────────────────────────────────
        ret_domain = [
            ('x_is_return_transfer', '=', True),
            ('state', 'not in', ('done', 'cancel')),
        ]
        if analytic:
            ret_domain.append(('x_issuance_project_id', '=', analytic.id))
        pending_returns = Picking.search(ret_domain, limit=20)

        # ── Site-to-site transfers ──────────────────────────────────────────
        xfer_domain = [('x_transfer_purpose', '=', 'site_to_site')]
        if analytic:
            xfer_domain += [
                '|',
                ('x_issuance_project_id', '=', analytic.id),
                ('x_dest_project_id', '=', analytic.id),
            ]
        transfers = Picking.search(
            xfer_domain + [('x_site_transfer_state', '!=', 'done')],
            order='id desc', limit=10,
        )
        dest_receipts = Picking.search([
            ('x_is_dest_receipt', '=', True),
            ('state', 'not in', ('done', 'cancel')),
            ('x_issuance_project_id', '=', analytic.id if analytic else 0),
        ])

        # ── Critical stock alert ────────────────────────────────────────────
        alert_vals = {
            'has_stock_alert': False,
            'alert_product_id': False,
            'alert_qty_on_hand': 0.0,
            'alert_min_qty': 0.0,
            'alert_message': False,
        }
        if warehouse:
            orderpoints = Orderpoint.search([
                ('warehouse_id', '=', warehouse.id),
            ], limit=50)
            for op in orderpoints:
                on_hand = op.product_id.with_context(
                    location=warehouse.lot_stock_id.id
                ).qty_available
                if on_hand < op.product_min_qty:
                    alert_vals = {
                        'has_stock_alert': True,
                        'alert_product_id': op.product_id.id,
                        'alert_qty_on_hand': on_hand,
                        'alert_min_qty': op.product_min_qty,
                        'alert_message': _(
                            '%(product)s — on hand %(qty).2f (minimum %(min).2f)'
                        ) % {
                            'product': op.product_id.display_name,
                            'qty': on_hand,
                            'min': op.product_min_qty,
                        },
                    }
                    break

        dashboard.with_context(_refreshing_dashboard=True).write({
            'name': analytic.name if analytic else _('Site Store Dashboard'),
            'project_analytic_account_id': analytic.id if analytic else False,
            'project_name': analytic.name if analytic else _('No project assigned'),
            'user_name': user.name,
            'open_pr_count': len(open_prs),
            'pending_signature_count': len(pending_sig),
            'arrivals_today_count': len(arrivals_today),
            'partial_receipts_count': len(partial_receipts),
            'issuance_mtd_count': len(issuances_mtd),
            'pending_returns_count': len(pending_returns),
            'pending_transfer_count': len(transfers),
            'dest_receipt_count': len(dest_receipts),
            'normal_issuance_pct': normal_pct,
            'subcontractor_issuance_pct': sub_pct,
            'pr_ids': [(6, 0, open_prs.ids)],
            'pending_receipt_ids': [(6, 0, pending_receipts.ids)],
            'recent_grn_ids': [(6, 0, recent_grns.ids)],
            'on_order_po_ids': [(6, 0, on_order.ids)],
            'transfer_ids': [(6, 0, transfers.ids)],
            'recent_issuance_ids': [(6, 0, recent_issuances.ids)],
            'pending_return_ids': [(6, 0, pending_returns.ids)],
            **alert_vals,
        })

    @api.model
    def action_open_dashboard(self):
        """Open or refresh the site store dashboard.

        Admins and Procurement Officers are redirected to the Procurement HO
        dashboard so that they always land on a single, unified operations view.
        """
        user = self.env.user
        # Admin / HO users → redirect to the Procurement HO dashboard
        if user._matracon_is_admin() or (
            not user.has_group('purchase_demand_raise.group_site_store')
            and user._matracon_is_procurement_officer()
        ):
            return self.env['x.procurement.ho.dashboard'].action_open_dashboard()
        if not user.has_group('purchase_demand_raise.group_site_store'):
            raise UserError(_('Only Site Store users can open this dashboard.'))
        dashboard = self.create({})
        self._refresh_dashboard_data(dashboard)
        return {
            'type': 'ir.actions.act_window',
            'name': dashboard.name or _('Site Store Dashboard'),
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

    def action_raise_pr(self):
        return {
            'type': 'ir.actions.act_window',
            'name': _('New Purchase Requisition'),
            'res_model': 'purchase.order',
            'view_mode': 'form',
            'target': 'current',
            'context': {
                'default_x_is_pr_document': True,
                'default_x_pr_state': 'draft',
            },
        }

    def action_open_prs(self):
        analytic = self._project_analytic()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Purchase Requisitions'),
            'res_model': 'purchase.order',
            'view_mode': 'list,form',
            'domain': self._project_domain_po(analytic),
            'context': {'default_x_is_pr_document': True},
        }

    def action_open_receipts(self):
        analytic = self._project_analytic()
        warehouse = self.env.user.x_default_warehouse_id
        domain = [
            ('picking_type_code', '=', 'incoming'),
            ('state', 'not in', ('done', 'cancel')),
        ]
        if warehouse:
            domain.append(('picking_type_id.warehouse_id', '=', warehouse.id))
        if analytic:
            domain.append(
                ('purchase_id.x_project_analytic_account_id', '=', analytic.id))
        return {
            'type': 'ir.actions.act_window',
            'name': _('Receipts To Validate'),
            'res_model': 'stock.picking',
            'view_mode': 'list,form',
            'domain': domain,
        }

    def action_open_issuance(self):
        self.ensure_one()
        analytic = self._project_analytic()
        domain = [('x_transfer_purpose', '=', 'material_issuance')]
        if analytic:
            domain.append(('x_issuance_project_id', '=', analytic.id))
        list_view = self.env.ref('site_operations.view_material_issuance_list').id
        return {
            'type': 'ir.actions.act_window',
            'name': _('Material Issuance'),
            'res_model': 'stock.picking',
            'view_mode': 'list,form',
            'views': [(list_view, 'list'), (False, 'form')],
            'domain': domain,
            'context': {
                'default_x_transfer_purpose': 'material_issuance',
                'default_x_generate_gate_pass': True,
                'default_x_issuance_project_id': analytic.id if analytic else False,
                'search_default_filter_in_progress': 1,
            },
        }

    def action_open_transfers(self):
        self.ensure_one()
        analytic = self._project_analytic()
        domain = [('x_transfer_purpose', '=', 'site_to_site')]
        if analytic:
            domain += [
                '|',
                ('x_issuance_project_id', '=', analytic.id),
                ('x_dest_project_id', '=', analytic.id),
            ]
        list_view = self.env.ref('site_operations.view_material_issuance_list').id
        return {
            'type': 'ir.actions.act_window',
            'name': _('Site-to-Site Transfers'),
            'res_model': 'stock.picking',
            'view_mode': 'list,form',
            'views': [(list_view, 'list'), (False, 'form')],
            'domain': domain,
            'context': {
                'default_x_transfer_purpose': 'site_to_site',
                'default_x_issuance_project_id': analytic.id if analytic else False,
                'search_default_filter_in_progress': 1,
            },
        }

    def action_open_returns(self):
        analytic = self._project_analytic()
        domain = [
            ('x_is_return_transfer', '=', True),
            ('state', 'not in', ('done', 'cancel')),
        ]
        if analytic:
            domain.append(('x_issuance_project_id', '=', analytic.id))
        return {
            'type': 'ir.actions.act_window',
            'name': _('Pending Returns'),
            'res_model': 'stock.picking',
            'view_mode': 'list,form',
            'domain': domain,
        }
