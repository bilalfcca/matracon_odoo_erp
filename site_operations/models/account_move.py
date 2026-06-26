from dateutil.relativedelta import relativedelta
from markupsafe import Markup

from odoo import models, fields, api, _
from odoo.exceptions import UserError

from . import matracon_notifications as matracon_notify


class AccountMoveSiteOps(models.Model):
    """Vendor bills: PO link, liability sheet, project balance, notifications."""
    _inherit = 'account.move'

    @api.model
    def _register_hook(self):
        self.env.cr.execute("""
            ALTER TABLE account_move
                ADD COLUMN IF NOT EXISTS x_project_analytic_account_id  INTEGER,
                ADD COLUMN IF NOT EXISTS x_liability_registered          BOOLEAN
                    NOT NULL DEFAULT FALSE,
                ADD COLUMN IF NOT EXISTS x_liability_amount_registered   DOUBLE PRECISION
                    NOT NULL DEFAULT 0.0,
                ADD COLUMN IF NOT EXISTS x_purchase_order_id             INTEGER,
                ADD COLUMN IF NOT EXISTS x_liability_sheet_id            INTEGER,
                ADD COLUMN IF NOT EXISTS x_wht_tax_id                    INTEGER,
                ADD COLUMN IF NOT EXISTS x_fbr_payment_id                INTEGER
        """)
        return super()._register_hook()

    x_project_analytic_account_id = fields.Many2one(
        'account.analytic.account',
        string='Project (Site)',
        tracking=True,
        help='Project for liability sheet and fund tracking.',
    )
    x_purchase_order_id = fields.Many2one(
        'purchase.order', string='Purchase Order', tracking=True, copy=False,
        domain=[('state', 'in', ('purchase', 'done'))],
    )
    x_liability_sheet_id = fields.Many2one(
        'x.liability.sheet', string='Liability Sheet', readonly=True, copy=False)
    x_liability_registered = fields.Boolean(
        string='Liability Registered', default=False, readonly=True, copy=False)
    x_liability_amount_registered = fields.Float(
        string='Liability Amount Registered', default=0.0, readonly=True, copy=False)
    x_wht_tax_id = fields.Many2one(
        'account.tax', string='Withholding Tax (WHT)',
        domain="[('type_tax_use', '=', 'purchase'), ('active', '=', True)]",
        help='If set, a draft FBR payment is created when the bill is posted.',
    )
    x_fbr_payment_id = fields.Many2one(
        'account.payment', string='FBR WHT Payment Draft', readonly=True, copy=False)

    vendor_bill_count = fields.Integer(compute='_compute_linked_counts')
    liability_sheet_count = fields.Integer(compute='_compute_linked_counts')
    picking_count = fields.Integer(compute='_compute_linked_counts')

    @api.depends('x_liability_sheet_id', 'x_purchase_order_id')
    def _compute_linked_counts(self):
        for move in self:
            move.liability_sheet_count = 1 if move.x_liability_sheet_id else 0
            move.picking_count = len(move._get_linked_pickings())
            move.vendor_bill_count = 0

    def _get_linked_pickings(self):
        self.ensure_one()
        if self.x_purchase_order_id:
            return self.env['stock.picking'].search([
                ('purchase_id', '=', self.x_purchase_order_id.id),
                ('picking_type_code', '=', 'incoming'),
            ])
        return self.env['stock.picking']

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('move_type') == 'in_invoice':
                self._matracon_apply_vendor_bill_defaults(vals)
        moves = super().create(vals_list)
        for move in moves.filtered(lambda m: m.move_type == 'in_invoice'):
            move._ensure_liability_sheet_for_bill(notify=False)
        return moves

    def write(self, vals):
        res = super().write(vals)
        if any(k in vals for k in (
            'x_project_analytic_account_id', 'partner_id', 'amount_total',
            'x_purchase_order_id', 'state',
        )):
            for move in self.filtered(
                lambda m: m.move_type == 'in_invoice' and m.state == 'draft'
            ):
                move._ensure_liability_sheet_for_bill(notify=False)
        return res

    @api.model
    def _matracon_apply_vendor_bill_defaults(self, vals):
        user = self.env.user
        if not vals.get('x_project_analytic_account_id'):
            if vals.get('x_purchase_order_id'):
                po = self.env['purchase.order'].browse(vals['x_purchase_order_id'])
                if po.x_project_analytic_account_id:
                    vals['x_project_analytic_account_id'] = (
                        po.x_project_analytic_account_id.id)
            elif user.x_default_analytic_account_id:
                vals['x_project_analytic_account_id'] = (
                    user.x_default_analytic_account_id.id)
        if vals.get('x_purchase_order_id') and not vals.get('partner_id'):
            po = self.env['purchase.order'].browse(vals['x_purchase_order_id'])
            if po.partner_id:
                vals['partner_id'] = po.partner_id.id
        if vals.get('x_purchase_order_id') and not vals.get('invoice_origin'):
            po = self.env['purchase.order'].browse(vals['x_purchase_order_id'])
            vals['invoice_origin'] = po.name

    @api.onchange('x_purchase_order_id')
    def _onchange_x_purchase_order_id(self):
        if self.x_purchase_order_id:
            self.partner_id = self.x_purchase_order_id.partner_id
            self.x_project_analytic_account_id = (
                self.x_purchase_order_id.x_project_analytic_account_id)
            self.invoice_origin = self.x_purchase_order_id.name

    def action_post(self):
        for move in self.filtered(
            lambda m: m.move_type == 'in_invoice' and m.state != 'posted'
        ):
            move._matracon_apply_bill_analytic()
        res = super().action_post()
        for move in self.filtered(
            lambda m: m.move_type == 'in_invoice' and m.state == 'posted'
        ):
            move._ensure_liability_sheet_for_bill(notify=True)
            move._update_project_balance_from_bill()
            if move.x_wht_tax_id:
                move._create_fbr_wht_payment_draft()
        return res

    def button_draft(self):
        for move in self.filtered(
            lambda m: m.move_type == 'in_invoice' and m.x_liability_registered
        ):
            move._reverse_liability_sheet_from_bill()
        return super().button_draft()

    def _ensure_liability_sheet_for_bill(self, notify=True):
        """Create/update liability sheet for this vendor bill."""
        self.ensure_one()
        if self.move_type != 'in_invoice' or not self.partner_id:
            return
        if not self.x_project_analytic_account_id:
            if self.env.user.x_default_analytic_account_id:
                self.x_project_analytic_account_id = (
                    self.env.user.x_default_analytic_account_id)
            else:
                return

        amount = self.amount_total
        if not amount and self.invoice_line_ids:
            amount = sum(self.invoice_line_ids.mapped('price_total'))
        if not amount:
            return

        bill_date = self.invoice_date or fields.Date.today()
        month_start = bill_date.replace(day=1)
        month_end = (month_start + relativedelta(months=1)) - relativedelta(days=1)

        LiabilitySheet = self.env['x.liability.sheet'].sudo()
        sheet = LiabilitySheet.search([
            ('project_analytic_account_id', '=', self.x_project_analytic_account_id.id),
            ('date_from', '=', month_start),
            ('state', 'in', ['draft', 'submitted']),
        ], limit=1)

        created = False
        if not sheet:
            sheet = LiabilitySheet.create({
                'project_analytic_account_id': self.x_project_analytic_account_id.id,
                'date_from': month_start,
                'date_to': month_end,
            })
            created = True

        existing_line = sheet.line_ids.filtered(
            lambda l: l.partner_id.id == self.partner_id.id)
        desc = self.ref or self.name or _('Vendor Bill — %s') % self.partner_id.name

        if self.state == 'posted':
            if self.x_liability_registered and self.x_liability_sheet_id == sheet:
                return
            delta = amount
            if existing_line:
                existing_line[0].write({
                    'new_liability': existing_line[0].new_liability + delta,
                    'description': desc,
                })
            else:
                sheet.write({'line_ids': [(0, 0, {
                    'description': desc,
                    'partner_id': self.partner_id.id,
                    'new_liability': delta,
                })]})
            self.write({
                'x_liability_registered': True,
                'x_liability_amount_registered': amount,
                'x_liability_sheet_id': sheet.id,
            })
            self.message_post(body=Markup(_(
                'Liability Sheet <b>%(sheet)s</b> updated — vendor <b>%(vendor)s</b>: '
                '<b>+%(amount)s</b> in <i>New Liability (Bills)</i>.'
            )) % {
                'sheet': sheet.name,
                'vendor': self.partner_id.name,
                'amount': f'{amount:,.2f}',
            })
            if notify:
                accountants = matracon_notify.site_accountants_for_analytic(
                    self.env, self.x_project_analytic_account_id)
                matracon_notify.notify_users(
                    self,
                    accountants,
                    _('Vendor bill <b>%s</b> posted — liability sheet <b>%s</b> updated.')
                    % (self.name, sheet.name),
                    summary=_('Vendor Bill Posted'),
                )
        elif created:
            sheet.write({'line_ids': [(0, 0, {
                'description': desc,
                'partner_id': self.partner_id.id,
                'new_liability': 0.0,
            })]})
            self.x_liability_sheet_id = sheet.id
            self.message_post(body=Markup(_(
                'Liability Sheet <b>%s</b> auto-created for draft vendor bill.'
            )) % sheet.name)
            if notify:
                accountants = matracon_notify.site_accountants_for_analytic(
                    self.env, self.x_project_analytic_account_id)
                matracon_notify.notify_users(
                    self,
                    accountants,
                    _('Draft vendor bill <b>%s</b> — liability sheet <b>%s</b> created.')
                    % (self.name or _('New'), sheet.name),
                    summary=_('Vendor Bill / Liability Sheet'),
                )

    def _reverse_liability_sheet_from_bill(self):
        self.ensure_one()
        if not self.x_liability_registered or not self.x_liability_amount_registered:
            return
        amount = self.x_liability_amount_registered
        sheet = self.x_liability_sheet_id
        if sheet and sheet.state in ('draft', 'submitted'):
            for line in sheet.line_ids.filtered(
                lambda l: l.partner_id == self.partner_id
            ):
                line.write({
                    'new_liability': max(line.new_liability - amount, 0.0),
                })
        self.write({
            'x_liability_registered': False,
            'x_liability_amount_registered': 0.0,
        })

    def _matracon_apply_bill_analytic(self):
        """Tag vendor bill lines with project analytic for liability / balance tracking."""
        self.ensure_one()
        analytic = self.x_project_analytic_account_id
        if not analytic:
            return
        dist = {str(analytic.id): 100.0}
        lines = self.invoice_line_ids.filtered(lambda l: not l.display_type)
        if lines:
            lines.write({'analytic_distribution': dist})

    def _update_project_balance_from_bill(self):
        """Posted vendor bills increase project obligation (vendor liability metric)."""
        self.ensure_one()
        if not self.x_project_analytic_account_id:
            return
        project = self.env['project.project'].search([
            ('x_analytic_account_id', '=', self.x_project_analytic_account_id.id),
        ], limit=1)
        if project:
            project.invalidate_recordset([
                'x_total_vendor_liability', 'x_available_balance',
                'x_funds_received', 'x_total_spent',
            ])

    def _create_fbr_wht_payment_draft(self):
        """Draft outbound payment to FBR when WHT is set on vendor bill."""
        self.ensure_one()
        if self.x_fbr_payment_id or not self.x_wht_tax_id:
            return
        taxes = self.x_wht_tax_id.compute_all(
            self.amount_total,
            currency=self.currency_id,
            partner=self.partner_id,
        )
        wht_amount = abs(sum(t.get('amount', 0.0) for t in taxes.get('taxes', [])))
        if wht_amount <= 0:
            return
        fbr_partner = self.env['res.partner'].search([
            ('name', 'ilike', 'FBR'),
        ], limit=1)
        if not fbr_partner:
            fbr_partner = self.env['res.partner'].create({
                'name': 'FBR - Federal Board of Revenue',
                'supplier_rank': 1,
            })
        journal = self.env['account.journal'].search([
            ('type', '=', 'bank'),
            ('company_id', '=', self.company_id.id),
        ], limit=1)
        payment = self.env['account.payment'].create({
            'payment_type': 'outbound',
            'partner_type': 'supplier',
            'partner_id': fbr_partner.id,
            'amount': wht_amount,
            'journal_id': journal.id if journal else False,
            'x_destination_project_id': self.x_project_analytic_account_id.id,
            'x_source_project_ids': [(6, 0, [self.x_project_analytic_account_id.id])],
            'x_wht_tax_id': self.x_wht_tax_id.id,
            'x_gross_approved_amount': wht_amount,
            'ref': _('WHT for %s') % self.name,
        })
        self.x_fbr_payment_id = payment.id
        self.message_post(body=Markup(_(
            'FBR WHT payment draft <b>%s</b> created (%.2f).'
        )) % (payment.name, wht_amount))

    def action_view_liability_sheet(self):
        self.ensure_one()
        if not self.x_liability_sheet_id:
            self._ensure_liability_sheet_for_bill(notify=False)
        return {
            'type': 'ir.actions.act_window',
            'name': _('Liability Sheet'),
            'res_model': 'x.liability.sheet',
            'view_mode': 'form',
            'res_id': self.x_liability_sheet_id.id,
        }

    def action_view_purchase_order(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Purchase Order'),
            'res_model': 'purchase.order',
            'view_mode': 'form',
            'res_id': self.x_purchase_order_id.id,
        }

    def action_view_pickings(self):
        self.ensure_one()
        pickings = self._get_linked_pickings()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Receipts'),
            'res_model': 'stock.picking',
            'view_mode': 'list,form',
            'domain': [('id', 'in', pickings.ids)],
        }

    @api.model
    def _matracon_create_draft_bill_from_po_receipt(self, picking):
        """Create draft vendor bill when Site Store validates PO receipt."""
        po = picking.purchase_id
        if not po or not po.partner_id:
            return self.env['account.move']
        existing = self.search([
            ('x_purchase_order_id', '=', po.id),
            ('move_type', '=', 'in_invoice'),
            ('state', '=', 'draft'),
        ], limit=1)
        if existing:
            return existing

        line_vals = []
        for move in picking.move_ids.filtered(
            lambda m: m.product_id and m.quantity > 0
        ):
            line_vals.append((0, 0, {
                'product_id': move.product_id.id,
                'name': move.product_id.display_name,
                'quantity': move.quantity,
                'price_unit': move.product_id.standard_price or 0.0,
                'tax_ids': [(6, 0, move.product_id.supplier_taxes_id.ids)],
            }))
        if not line_vals:
            return self.env['account.move']

        bill = self.create({
            'move_type': 'in_invoice',
            'partner_id': po.partner_id.id,
            'invoice_date': fields.Date.context_today(self),
            'x_purchase_order_id': po.id,
            'x_project_analytic_account_id': (
                po.x_project_analytic_account_id.id
                if po.x_project_analytic_account_id else False),
            'invoice_origin': po.name,
            'ref': _('GRN %s') % picking.name,
            'invoice_line_ids': line_vals,
        })
        accountants = matracon_notify.site_accountants_for_analytic(
            self.env, bill.x_project_analytic_account_id)
        matracon_notify.notify_users(
            bill,
            accountants,
            _('Draft vendor bill <b>%s</b> auto-created from receipt <b>%s</b>.')
            % (bill.name or _('New'), picking.name),
            summary=_('Vendor Bill Ready for Review'),
        )
        matracon_notify.schedule_activity(
            bill,
            accountants,
            _('Review vendor bill for %s') % po.name,
            note=_('Receipt %s validated — vendor bill draft created.') % picking.name,
        )
        picking.message_post(body=Markup(_(
            'Draft vendor bill <b>%s</b> created for accountant review.'
        )) % (bill.name or _('New')))
        return bill
