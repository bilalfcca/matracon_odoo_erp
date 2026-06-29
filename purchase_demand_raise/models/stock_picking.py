from odoo import models, fields, api, _
from odoo.exceptions import UserError


class StockPickingPR(models.Model):
    _inherit = 'stock.picking'

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

    # ── Validate gate: require Gate Pass + Weight Document for PR receipts ────
    def button_validate(self):
        """Block validation until Gate Pass Inward No and Weight Document are
        provided for incoming receipts that come from a PR document."""
        for picking in self:
            if (picking.picking_type_code == 'incoming'
                    and picking.purchase_id
                    and picking.purchase_id.x_is_pr_document):
                missing = []
                if not picking.x_gate_pass_no:
                    missing.append(_('Gate Pass Inward No'))
                if not picking.x_weight_document:
                    missing.append(_('Weight Document'))
                if missing:
                    raise UserError(_(
                        'Please complete the following required field(s) before validating:\n\n• %s'
                    ) % '\n• '.join(missing))
        res = super().button_validate()
        self._matracon_after_receipt_validated()
        return res

    def _matracon_after_receipt_validated(self):
        """Hook: draft vendor bill + accountant notification after GRN."""
        for picking in self.filtered(
            lambda p: p.state == 'done'
            and p.picking_type_code == 'incoming'
            and p.purchase_id
        ):
            self.env['account.move']._matracon_create_draft_bill_from_po_receipt(picking)
