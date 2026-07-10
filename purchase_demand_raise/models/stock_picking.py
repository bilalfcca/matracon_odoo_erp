from odoo import models, fields, api, _
from odoo.exceptions import UserError


class StockPickingPR(models.Model):
    _inherit = 'stock.picking'

    # ── DMR No — auto-generated on creation, never editable ──────────────────
    x_dmr_no = fields.Char(
        string='DMR No',
        copy=False,
        readonly=True,
        help='Delivery Material Receipt number — auto-assigned when the receipt is created.',
    )

    # ── Gate Pass Inward No ───────────────────────────────────────────────────
    x_gate_pass_no = fields.Char(
        string='Gate Pass Inward No',
        copy=False,
        help='Physical gate pass number issued when the vehicle/material enters the site.',
    )

    # ── Weight Document ───────────────────────────────────────────────────────
    x_weight_document = fields.Binary(
        string='Weight Document',
        attachment=True,
        copy=False,
        help='Weighbridge / weight-slip proof for the delivered material.',
    )
    x_weight_document_filename = fields.Char(string='Weight Document Filename')

    # ── Procurement type mirror — lets the receipt form condition on this ─────
    x_is_site_procurement = fields.Boolean(
        string='Is Site Procurement',
        compute='_compute_is_site_procurement',
        store=False,
        help='True when the linked PO is a Site Procurement (no formal vendor required).',
    )

    @api.depends('purchase_id', 'purchase_id.x_procurement_type')
    def _compute_is_site_procurement(self):
        for picking in self:
            picking.x_is_site_procurement = (
                bool(picking.purchase_id)
                and picking.purchase_id.x_procurement_type == 'site_procurement'
            )

    # ── Project — auto-filled from the linked PO, never manually editable ────
    x_project_analytic_account_id = fields.Many2one(
        'account.analytic.account',
        string='PR Project',
        compute='_compute_project_from_po',
        store=True,
        readonly=True,
        copy=False,
        help='Auto-populated from the linked Purchase Order\'s project analytic account.',
    )

    @api.depends('purchase_id', 'purchase_id.x_project_analytic_account_id')
    def _compute_project_from_po(self):
        for picking in self:
            if picking.purchase_id:
                picking.x_project_analytic_account_id = (
                    picking.purchase_id.x_project_analytic_account_id
                )
            else:
                picking.x_project_analytic_account_id = False

    # ── Auto-assign DMR on incoming PR receipt creation ───────────────────────
    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        for picking in records:
            if (picking.picking_type_id.code == 'incoming'
                    and picking.origin
                    and not picking.x_dmr_no):
                picking.x_dmr_no = picking._matracon_assign_dmr()
        return records

    def _matracon_assign_dmr(self):
        """Return the next DMR number for this picking's site warehouse.
        Auto-creates the ir.sequence if the site hasn't been set up yet.
        """
        self.ensure_one()
        wh = self.picking_type_id.warehouse_id
        if wh and wh.code:
            seq_code = 'x.dmr.%s' % wh.code.strip().lower()
            prefix = 'DMR-%s-' % wh.code.strip().upper()
            seq_name = 'DMR — %s' % (wh.name or wh.code)
        else:
            seq_code = 'x.dmr'
            prefix = 'DMR-'
            seq_name = 'DMR — General'

        IrSeq = self.env['ir.sequence'].sudo()
        if not IrSeq.search([('code', '=', seq_code)], limit=1):
            IrSeq.create({
                'name': seq_name,
                'code': seq_code,
                'prefix': prefix,
                'padding': 4,
                'number_next': 1,
                'number_increment': 1,
                'company_id': self.company_id.id or self.env.company.id,
            })
        return IrSeq.next_by_code(seq_code) or '/'

    # ── Validate gate: require Weight Document for PR receipts ────────────────
    def button_validate(self):
        """Block validation until Weight Document is provided for incoming
        receipts that come from a PR document. Gate Pass Inward No is optional."""
        for picking in self:
            if (picking.picking_type_code == 'incoming'
                    and picking.purchase_id
                    and picking.purchase_id.x_is_pr_document):
                if not picking.x_weight_document:
                    raise UserError(_(
                        'Please upload the Weight/PO Document before marking the receipt as received.'
                    ))
        res = super().button_validate()
        self._matracon_after_receipt_validated()
        return res

    def _matracon_after_receipt_validated(self):
        """Hook: accounting document creation after GRN.

        - HO Procurement          → draft vendor bill.
        - Site Procurement
          + Add to Liability Sheet → draft vendor bill (uses x_site_vendor_id as partner).
          + No Liability Sheet     → draft petty cash expense.
        """
        for picking in self.filtered(
            lambda p: p.state == 'done'
            and p.picking_type_code == 'incoming'
            and p.purchase_id
        ):
            po = picking.purchase_id
            if po.x_procurement_type == 'site_procurement':
                if po.x_add_to_liability_sheet:
                    # Swap in the local supplier as partner so the bill creation works
                    if po.x_site_vendor_id and not po.partner_id:
                        po.sudo().write({'partner_id': po.x_site_vendor_id.id})
                    self.env['account.move']._matracon_create_draft_bill_from_po_receipt(picking)
                else:
                    self.env['x.petty.cash.expense']._matracon_create_from_po_receipt(picking)
            else:
                self.env['account.move']._matracon_create_draft_bill_from_po_receipt(picking)
