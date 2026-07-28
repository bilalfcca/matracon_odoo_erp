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
                ADD COLUMN IF NOT EXISTS x_fbr_payment_id                INTEGER,
                ADD COLUMN IF NOT EXISTS x_source_picking_id             INTEGER,
                ADD COLUMN IF NOT EXISTS x_bill_copy_filename            VARCHAR
        """)
        return super()._register_hook()

    x_project_analytic_account_id = fields.Many2one(
        'account.analytic.account',
        string='Project (Site)',
        tracking=True,
        help='Project for liability sheet and fund tracking.',
        default=lambda self: self.env.user.x_default_analytic_account_id,
    )

    # Non-stored: exposes the current user's site analytic so the invoice-line
    # account_id domain can filter the account picker for site accountants.
    # Returns False for HO/admin users → no restriction applied in the view domain.
    x_user_analytic_id = fields.Many2one(
        'account.analytic.account',
        compute='_compute_x_user_analytic_id',
        store=False,
        string='User Site Analytic',
    )

    def _compute_x_user_analytic_id(self):
        analytic = self.env.user.sudo().x_default_analytic_account_id
        for move in self:
            move.x_user_analytic_id = analytic

    # Non-stored: True when the current user belongs to any HO/admin group.
    # Used in the invoice-line account_id domain and context to remove type-based
    # pre-filtering for HO users while keeping it for site accountants.
    x_user_is_ho = fields.Boolean(
        compute='_compute_x_user_is_ho',
        store=False,
        string='User is HO',
    )

    @api.depends_context('uid')
    def _compute_x_user_is_ho(self):
        is_ho = (
            self.env.user.has_group('purchase_demand_raise.group_head_office')
            or self.env.user.has_group('site_operations.group_finance_ho')
        )
        for move in self:
            move.x_user_is_ho = is_ho

    x_purchase_order_id = fields.Many2one(
        'purchase.order', string='Purchase Order', tracking=True, copy=False,
        domain=[('state', 'in', ('purchase', 'done'))],
    )
    x_liability_sheet_id = fields.Many2one(
        'x.liability.sheet', string='Liability Sheet', readonly=True, copy=False)
    x_liability_registered = fields.Boolean(
        string='Liability Registered', default=False, readonly=True, copy=False)
    x_liability_amount_registered = fields.Float(
        string='Liability Amount Registered', default=0.0, readonly=True, copy=False, digits=(16, 0))
    x_source_picking_id = fields.Many2one(
        'stock.picking', string='Source Material Issuance',
        readonly=True, copy=False, index=True,
        help='Material issuance that generated this backcharge journal entry.',
    )
    x_bill_copy = fields.Binary(
        string='Bill Copy',
        attachment=True,
        copy=False,
        help='Attach a scanned / digital copy of the physical vendor bill.',
    )
    x_bill_copy_filename = fields.Char(string='Bill Copy Filename')
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
            # Auto-tag all lines (product + tax + payable) immediately on creation
            # so draft bills show the project analytic without requiring a manual step.
            if move.x_project_analytic_account_id and move.line_ids:
                move._matracon_apply_bill_analytic()
        return moves

    def write(self, vals):
        # Capture old project links BEFORE the write so that if a posted
        # customer invoice is re-assigned to a different project, the OLD
        # project's billed amount is also recalculated.
        pre_analytics = set()
        if 'x_project_analytic_account_id' in vals:
            pre_analytics = set(
                m.x_project_analytic_account_id.id
                for m in self
                if m.move_type in ('out_invoice', 'out_refund')
                and m.state == 'posted'
                and m.x_project_analytic_account_id
            )
        res = super().write(vals)
        if any(k in vals for k in (
            'x_project_analytic_account_id', 'partner_id', 'amount_total',
            'x_purchase_order_id', 'state',
        )):
            for move in self.filtered(
                lambda m: m.move_type == 'in_invoice' and m.state == 'draft'
            ):
                move._ensure_liability_sheet_for_bill(notify=False)
        # If x_project_analytic_account_id changed on any customer invoice,
        # recompute billed amount for both old and new project.
        if 'x_project_analytic_account_id' in vals:
            self._matracon_update_project_billed_amount(extra_analytic_ids=pre_analytics)
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

    @api.onchange('x_project_analytic_account_id')
    def _onchange_project_analytic_account_fill_lines(self):
        """When the project is (re-)selected on a vendor bill or customer invoice,
        immediately tag every existing journal item (product lines, tax lines,
        payable/receivable line) with the new analytic distribution so the Journal
        Entries tab and the Invoice Lines tab both reflect the correct project in
        real time.  Customer invoice lines are always read-only in the SA view so
        this just ensures the display is correct; the analytic is also stamped
        at DB-write time by AccountMoveLineSiteOps.create()."""
        if self.move_type not in ('in_invoice', 'out_invoice'):
            return
        dist = (
            {str(self.x_project_analytic_account_id.id): 100.0}
            if self.x_project_analytic_account_id else {}
        )
        for line in self.line_ids:
            line.analytic_distribution = dist

    @api.onchange('invoice_line_ids')
    def _onchange_invoice_line_ids_fill_analytic(self):
        """When a new product line is added, immediately fill its analytic so
        the user sees it in the UI while still editing.

        We iterate invoice_line_ids (not line_ids) because at onchange time
        Odoo's base _onchange_invoice_line_ids has not yet rebuilt the full
        line_ids; auto-generated tax and payable lines are handled at DB-write
        time by AccountMoveLineSiteOps.create() further below."""
        if not self.x_project_analytic_account_id or self.move_type not in ('in_invoice', 'out_invoice'):
            return
        dist = {str(self.x_project_analytic_account_id.id): 100.0}
        for line in self.invoice_line_ids:
            if not line.analytic_distribution:
                line.analytic_distribution = dist

    @api.onchange('line_ids')
    def _onchange_line_ids_fill_analytic_for_entry(self):
        """For site-accountant journal entries (move_type='entry'): fill
        analytic_distribution on every new line immediately so the read-only
        Analytic column shows the project without requiring a manual save.

        invoice_line_ids does not apply to MISC entries — we use line_ids
        directly here.  The DB-level create() hook below handles the same
        for server-side and programmatic record creation."""
        if (
            self.move_type != 'entry'
            or not self.x_project_analytic_account_id
            or not self.env.user.has_group('site_operations.group_site_accountant')
        ):
            return
        dist = {str(self.x_project_analytic_account_id.id): 100.0}
        for line in self.line_ids:
            if not line.analytic_distribution:
                line.analytic_distribution = dist

    def _matracon_update_project_billed_amount(self, extra_analytic_ids=()):
        """Recompute x_billed_to_client on every project linked to a customer
        invoice (out_invoice / out_refund) in this recordset.

        ``extra_analytic_ids`` allows passing analytic IDs that were linked
        *before* a write so that projects losing their invoice link are also
        updated (e.g. when x_project_analytic_account_id is changed on an
        already-posted invoice).
        """
        analytic_ids = set(extra_analytic_ids)
        for m in self:
            if (
                m.move_type in ('out_invoice', 'out_refund')
                and m.x_project_analytic_account_id
            ):
                analytic_ids.add(m.x_project_analytic_account_id.id)
        if not analytic_ids:
            return
        projects = self.env['project.project'].sudo().search([
            ('x_analytic_account_id', 'in', list(analytic_ids)),
        ])
        if projects:
            projects._compute_billed_to_client()
            # _compute_financial_completion_pct depends on x_billed_to_client;
            # call it explicitly to flush all dependent stored fields at once.
            projects._compute_financial_completion_pct()

    def action_post(self):
        # ── Auto-fill invoice_date_due so payment-term lines always carry a date ──
        # Due dates are hidden in the UI for both site accountants (vendor bills)
        # and all users (customer invoices).  Ensure the field is always populated
        # before posting so Odoo's posting flow can set date_maturity on the
        # payment_term line correctly.
        for move in self.filtered(lambda m: m.is_invoice() and not m.invoice_date_due):
            move.invoice_date_due = move.invoice_date or fields.Date.context_today(self)

        # ── For customer invoices: also stamp date_maturity on any existing
        #    payment_term lines that still have no due date (draft state can
        #    create these when invoice_payment_term_id is cleared) so the
        #    Odoo posting flow does not encounter a NULL date_maturity. ──────
        for move in self.filtered(lambda m: m.move_type == 'out_invoice'):
            due = (
                move.invoice_date_due
                or move.invoice_date
                or fields.Date.context_today(self)
            )
            pt_lines = move.line_ids.filtered(
                lambda l: l.display_type == 'payment_term' and not l.date_maturity
            )
            if pt_lines:
                pt_lines.write({'date_maturity': due})

        # ── Bill Copy mandatory for vendor bills (not system-generated backcharges) ──
        # Skip during module installation / demo-data loading so that Odoo's own
        # demo fixtures (account_demo.py) are not blocked by this custom check.
        # registry._init covers the install phase; install_mode=True in context
        # covers _post_load_demo_data which is called after _init is cleared.
        if not self.env.registry._init and not self.env.context.get('install_mode'):
            missing_attachment = self.filtered(
                lambda m: (
                    m.move_type == 'in_invoice'
                    and m.state != 'posted'
                    and not m.x_source_picking_id   # skip auto-generated backcharge bills
                    and not m.x_bill_copy
                )
            )
        else:
            missing_attachment = self.browse()  # empty recordset — no check during init
        if missing_attachment:
            names = ', '.join(
                m.name or _('New') for m in missing_attachment
            )
            raise UserError(_(
                'Bill Copy attachment is mandatory before posting a Vendor Bill.\n\n'
                'Please upload the physical bill document for: %s'
            ) % names)

        # ── HO/Finance JE validation: payable lines must carry an analytic ──────
        # Without analytic_distribution on the payable line, a Head Office or
        # Finance journal entry cannot be attributed to any project, so it would
        # be invisible in dashboards and IPC calculations.
        # SA JEs are auto-filled by _onchange_line_ids_fill_analytic_for_entry;
        # this check is a safety net specifically for HO/Finance users.
        ho_or_finance = (
            self.env.user.has_group('purchase_demand_raise.group_head_office')
            or self.env.user.has_group('site_operations.group_finance_ho')
        )
        if ho_or_finance:
            for move in self.filtered(lambda m: m.move_type == 'entry' and m.state != 'posted'):
                missing = move.line_ids.filtered(
                    lambda l: (
                        l.account_id.account_type == 'liability_payable'
                        and l.partner_id
                        and not l.analytic_distribution
                    )
                )
                if missing:
                    partner_names = ', '.join(
                        l.partner_id.display_name or l.account_id.display_name
                        for l in missing
                    )
                    raise UserError(_(
                        'Please set an Analytic Account on all payable lines before posting.\n\n'
                        'Head Office / Finance journal entries must specify a project analytic '
                        'account so the payment is tracked in the correct project dashboard '
                        'and IPC calculations.\n\n'
                        'Missing analytic on lines for: %s'
                    ) % partner_names)

        # Site accountants have group_account_readonly (for Partner Ledger /
        # reporting menus) but Odoo's action_post() hard-checks group_account_invoice.
        # These are mutually exclusive Odoo 19 privilege levels; we cannot grant both.
        # Run the parent call via sudo() for site accountants — our bill copy check
        # above already validated business rules; sudo() only bypasses the group gate.
        if self.env.user.has_group('site_operations.group_site_accountant'):
            res = super(AccountMoveSiteOps, self.sudo()).action_post()
        else:
            res = super().action_post()
        for move in self.filtered(
            lambda m: m.move_type == 'in_invoice' and m.state == 'posted'
        ):
            # Apply project analytic AFTER posting so Odoo's own line recompute
            # (tax lines, payment-term lines) cannot overwrite the distribution.
            # Backcharge-generated bills are excluded — their picking sets analytics.
            if not move.x_source_picking_id:
                # Last-chance analytic auto-fill: if the bill was created or
                # imported without a project analytic (e.g. Excel import, or
                # created by a user with no default), stamp it from the current
                # (posting) user's default BEFORE applying analytics to lines.
                # We use direct SQL here because the move is already 'posted'
                # and Odoo's write lock would otherwise block a normal ORM write.
                if not move.x_project_analytic_account_id:
                    user_analytic = self.env.user.x_default_analytic_account_id
                    if user_analytic:
                        self.env.cr.execute(
                            "UPDATE account_move"
                            "   SET x_project_analytic_account_id = %s"
                            " WHERE id = %s",
                            (user_analytic.id, move.id),
                        )
                        move.invalidate_recordset(['x_project_analytic_account_id'])
                move.sudo()._matracon_apply_bill_analytic()
                move._ensure_liability_sheet_for_bill(notify=True)
                move._update_project_balance_from_bill()
            if move.x_wht_tax_id:
                move._create_fbr_wht_payment_draft()
        # Recompute project billed amount for any customer invoices just posted
        self._matracon_update_project_billed_amount()

        # Auto-sync liability sheets for direct journal entries (MISC etc.) that
        # carry payable lines with analytic distribution.  Vendor bills are already
        # handled above; backcharge-generated entries are excluded via x_source_picking_id.
        for move in self.filtered(
            lambda m: m.move_type == 'entry'
            and m.state == 'posted'
            and not m.x_source_picking_id
        ):
            move._ensure_liability_from_journal_entry()

        return res

    def button_draft(self):
        # Capture customer-invoice analytics BEFORE state changes to draft so
        # projects that lose a posted invoice are correctly updated afterwards.
        pre_analytics = set(
            m.x_project_analytic_account_id.id
            for m in self
            if m.move_type in ('out_invoice', 'out_refund')
            and m.state == 'posted'
            and m.x_project_analytic_account_id
        )
        for move in self.filtered(
            lambda m: m.move_type == 'in_invoice' and m.x_liability_registered
        ):
            move._reverse_liability_sheet_from_bill()
        res = super().button_draft()
        # Recompute after draft: pass pre-reset analytics so the compute runs
        # even though the invoices are no longer 'posted'.
        self._matracon_update_project_billed_amount(extra_analytic_ids=pre_analytics)
        return res

    # ── Customer-invoice sequence: include project/site code ─────────────────

    def _get_starting_sequence(self):
        """Include the project analytic account's code in the customer invoice
        sequence so invoices are numbered per-site like other documents.

        Standard:       INV/2026/00000
        With project:   INV/MCH/2026/00000   (MCH = project code / first word of name)

        Vendor bills, refunds and other move types are unaffected.
        """
        seq = super()._get_starting_sequence()
        if self.move_type == 'out_invoice' and self.x_project_analytic_account_id:
            project = self.x_project_analytic_account_id
            # Prefer the analytic account's short code; fall back to first word of name
            raw = (project.code.strip() if project.code else project.name.split()[0])
            site_code = raw.upper()[:8].replace(' ', '-')
            # "INV/2026/00000" → ["INV", "2026", "00000"] → "INV/MCH/2026/00000"
            parts = seq.split('/')
            parts.insert(1, site_code)
            seq = '/'.join(parts)
        return seq

    # (Real-time last-row balance is handled by the onchange on account.move.line
    #  itself — see account_move_line.py _onchange_amount_rebalance_last_row.
    #  A line-level onchange fires immediately when the user edits any debit/credit
    #  cell, whereas a parent @api.onchange('line_ids') only fires on row add/remove.)

    def _ensure_liability_sheet_for_bill(self, notify=True):
        """Create/update liability sheet for this vendor bill.

        Skipped for backcharge-generated vendor bills/credit notes (x_source_picking_id set):
        the stock.picking already manages the liability sheet on validation.
        """
        self.ensure_one()
        # Backcharge-generated docs are handled by stock_picking._auto_*_liability_sheet
        if self.x_source_picking_id:
            return
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

        today = fields.Date.today()
        month_start = today.replace(day=1)
        month_end = (month_start + relativedelta(months=1)) - relativedelta(days=1)

        LiabilitySheet = self.env['x.liability.sheet'].sudo()
        # Find the current active (non-paid) sheet for this project — regardless of
        # which month it covers.  One sheet per project stays open until FO marks it
        # paid; only then does the next sheet get auto-created with opening balances.
        sheet = LiabilitySheet.search([
            ('project_analytic_account_id', '=', self.x_project_analytic_account_id.id),
            ('state', 'not in', ['paid']),
        ], order='date_from desc', limit=1)

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
        # Description always comes from the contact's x_description, not from the
        # bill reference — so the label stays stable regardless of which entry
        # (bill, IPC, material issuance, etc.) last touched the line.
        partner_desc = (
            self.partner_id.x_description or self.partner_id.name or ''
        ).strip()

        if self.state == 'posted':
            if self.x_liability_registered and self.x_liability_sheet_id == sheet:
                return

            # Sheet is CEO-approved (locked) — link bill for reference but don't
            # touch locked lines.  The SA will run "Refresh from Ledger" on the
            # next sheet after this one is marked paid.
            if sheet.state == 'approved':
                self.x_liability_sheet_id = sheet.id
                return

            delta = amount
            if existing_line:
                existing_line[0].write({
                    'new_liability': existing_line[0].new_liability + delta,
                    # description is intentionally NOT updated — it stays as set
                    # from the contact record and is never overwritten by a bill ref.
                })
            else:
                sheet.write({'line_ids': [(0, 0, {
                    'description': partner_desc,
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
                'description': partner_desc,
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
        """Tag ALL vendor bill move lines (expense + payable) with project analytic.

        Tagging the payable line is critical: without it the standard Odoo aged
        payables report cannot filter / group by project, and project_project's
        AML fallback query for x_total_vendor_liability won't match either.

        In Odoo 19 the display_type for product lines is 'product' (not False),
        so we filter explicitly for 'product' — not with `not l.display_type`.
        """
        self.ensure_one()
        analytic = self.x_project_analytic_account_id
        if not analytic:
            return
        dist = {str(analytic.id): 100.0}
        # Tag ALL journal items: product lines, tax lines, payable line.
        # In Odoo 19 display_type is 'product' for expense lines and 'tax' for
        # auto-generated tax lines; both must carry the project analytic so that
        # aged payables, tax reports, and project balance queries all filter
        # correctly.  'payment_term' lines (receivable/payable counterpart) are
        # included deliberately so aged-payables by project works too.
        if self.line_ids:
            self.line_ids.write({'analytic_distribution': dist})

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

    def _ensure_liability_from_journal_entry(self):
        """Auto-create or update the liability sheet when a direct journal entry
        (move_type='entry', e.g. MISC) is posted that has payable lines with
        analytic_distribution set.

        Unlike vendor bills (which populate x_project_analytic_account_id on the
        move header), plain journal entries only carry the project via the line-level
        analytic_distribution JSON field.  This method reads that field, finds/creates
        the matching liability sheet for the entry's month, and computes accurate
        opening and period amounts from the full partner ledger.
        """
        self.ensure_one()

        payable_lines = self.line_ids.filtered(
            lambda l: (
                l.account_id.account_type == 'liability_payable'
                and l.partner_id
                and l.analytic_distribution
            )
        )
        if not payable_lines:
            return

        entry_date = self.date or fields.Date.today()
        month_start = entry_date.replace(day=1)
        month_end = (month_start + relativedelta(months=1)) - relativedelta(days=1)
        LiabilitySheet = self.env['x.liability.sheet'].sudo()
        AML = self.env['account.move.line'].sudo()

        # Collect {analytic_id: set(partner_ids)} from the payable lines
        analytic_partners = {}
        for aml in payable_lines:
            for analytic_id_str in (aml.analytic_distribution or {}):
                aid = int(analytic_id_str)
                analytic_partners.setdefault(aid, set()).add(aml.partner_id.id)

        for analytic_id, partner_ids in analytic_partners.items():
            analytic = self.env['account.analytic.account'].sudo().browse(analytic_id)
            if not analytic.exists():
                continue

            # Find the current active (non-paid) sheet for this project.
            # One sheet stays open until FO marks it paid — month boundary is ignored.
            sheet = LiabilitySheet.search([
                ('project_analytic_account_id', '=', analytic_id),
                ('state', 'not in', ['paid']),
            ], order='date_from desc', limit=1)
            if not sheet:
                sheet = LiabilitySheet.create({
                    'project_analytic_account_id': analytic_id,
                    'date_from': month_start,
                    'date_to': month_end,
                })

            # If sheet is CEO-approved (lines locked) skip line updates — the
            # amounts will be captured on the next sheet via Refresh from Ledger.
            if sheet.state == 'approved':
                continue

            str_analytic_id = str(analytic_id)

            def _in_project(l, _aid=analytic_id, _said=str_analytic_id):
                if l.move_id.x_project_analytic_account_id.id == _aid:
                    return True
                return _said in (l.analytic_distribution or {})

            for partner_id in partner_ids:
                # Compute accurate amounts from the full partner ledger
                candidate = AML.search([
                    ('partner_id', '=', partner_id),
                    ('move_id.state', '=', 'posted'),
                    ('account_id.account_type', '=', 'liability_payable'),
                ]).filtered(_in_project)

                opening_lines = candidate.filtered(
                    lambda l: l.move_id.date < sheet.date_from and not l.reconciled
                )
                opening = max(0.0, sum(l.credit - l.debit for l in opening_lines))

                period_lines = candidate.filtered(
                    lambda l: sheet.date_from <= l.move_id.date <= sheet.date_to
                )
                new_liab = sum(l.credit - l.debit for l in period_lines)

                existing_line = sheet.line_ids.filtered(
                    lambda l: l.partner_id.id == partner_id
                )
                if existing_line:
                    existing_line[0].write({
                        'opening_balance': round(opening, 2),
                        'new_liability': round(new_liab, 2),
                    })
                else:
                    partner_rec = self.env['res.partner'].browse(partner_id)
                    sheet.write({'line_ids': [(0, 0, {
                        'partner_id': partner_id,
                        'description': (
                            partner_rec.x_description or partner_rec.name or ''
                        ).strip(),
                        'opening_balance': round(opening, 2),
                        'new_liability': round(new_liab, 2),
                    })]})

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
        # System automation — site store must not need accounting/config ACLs.
        return self.sudo()._matracon_create_draft_bill_from_po_receipt_impl(picking)

    def _matracon_create_draft_bill_from_po_receipt_impl(self, picking):
        """Implementation (runs as sudo)."""
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
            'ref': 'GRN-%s' % picking.name,
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

    def action_print_journal_entry_voucher(self):
        """Open the Journal Entry Voucher PDF for posted journal entries."""
        return self.env.ref(
            'site_operations.action_report_journal_entry_voucher'
        ).report_action(self)


class AccountMoveLineSiteOps(models.Model):
    """DB-level hook: auto-fill analytic distribution on every vendor bill line.

    Why this is needed:
    - @api.onchange fires in the browser (virtual record) and is unreliable for
      auto-generated tax / payable lines because Odoo's base
      _onchange_invoice_line_ids runs AFTER ours and recreates those lines
      without our analytic.
    - Setting the distribution in vals *before* super().create() is the only
      reliable way to ensure ALL line types (product, tax, payment_term) carry
      the project analytic from the moment they hit the database, even when
      Odoo's own accounting recompute creates them automatically.
    """
    _inherit = 'account.move.line'

    @api.model_create_multi
    def create(self, vals_list):
        # Cache move objects to avoid repeated browses for the same move.
        move_cache = {}
        for vals in vals_list:
            if vals.get('analytic_distribution'):
                continue  # already set — respect explicit caller value
            move_id = vals.get('move_id')
            if not move_id:
                continue
            if move_id not in move_cache:
                move_cache[move_id] = self.env['account.move'].browse(move_id)
            move = move_cache[move_id]
            is_sa_entry = (
                move.move_type == 'entry'
                and self.env.user.has_group('site_operations.group_site_accountant')
            )
            if (
                (move.move_type in ('in_invoice', 'out_invoice') or is_sa_entry)
                and move.state == 'draft'
                and move.x_project_analytic_account_id
            ):
                vals['analytic_distribution'] = {
                    str(move.x_project_analytic_account_id.id): 100.0
                }
        return super().create(vals_list)
