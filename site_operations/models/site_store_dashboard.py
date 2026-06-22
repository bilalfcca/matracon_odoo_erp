"""Site Store Manager dashboard — matches Management Dashboard mockup in site_store_flow.docx."""

from datetime import timedelta

from odoo import models, fields, api, _
from odoo.exceptions import UserError


class SiteStoreDashboard(models.TransientModel):
    _name = 'x.site.store.dashboard'
    _description = 'Site Store Manager Dashboard'

    # ── Context header ────────────────────────────────────────────────────────
    project_analytic_account_id = fields.Many2one(
        'account.analytic.account', string='Project', readonly=True)
    project_name = fields.Char(string='Project Name', readonly=True)
    user_name = fields.Char(string='Store Manager', readonly=True)

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

        po_base = self._project_domain_po(analytic)

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
                m.product_uom_qty > m.quantity for m in p.move_ids if not m.display_type
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

        dashboard.write({
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
        """Open or refresh the site store dashboard."""
        if not self.env.user.has_group('purchase_demand_raise.group_site_store'):
            raise UserError(_('Only Site Store users can open this dashboard.'))
        dashboard = self.create({})
        self._refresh_dashboard_data(dashboard)
        return {
            'type': 'ir.actions.act_window',
            'name': _('Site Store Dashboard'),
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
        return self.env.ref(
            'site_operations.action_material_issuance_only').read()[0]

    def action_open_transfers(self):
        return self.env.ref(
            'site_operations.action_site_to_site_transfers').read()[0]

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
