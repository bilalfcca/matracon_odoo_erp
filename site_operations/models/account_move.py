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
                ADD COLUMN IF NOT EXISTS x_bill_copy_filename            VARCHAR,
                ADD COLUMN IF NOT EXISTS x_cheque_number                 VARCHAR,
                ADD COLUMN IF NOT EXISTS x_account_title                 VARCHAR,
                ADD COLUMN IF NOT EXISTS x_ho_source_project_id         INTEGER,
                ADD COLUMN IF NOT EXISTS x_ho_dest_project_id           INTEGER
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

    # Filtered One2many used in the Journal Items tab on vendor bills /
    # customer invoices.  Excludes 'payment_term' display_type lines —
    # those are the AP/AR counterpart lines Odoo auto-generates to balance
    # the entry.  Users cannot delete them (ValidationError) and should not
    # see them; only the lines they explicitly added should appear.
    x_journal_item_ids = fields.One2many(
        'account.move.line', 'move_id',
        string='Journal Items',
        domain=[('display_type', '!=', 'payment_term')],
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

    # ── Cheque / payment info (used on journal entries that represent direct payments)
    x_cheque_number = fields.Char(
        string='Cheque / Reference No.',
        tracking=True,
        help='Cheque or RTGS/NEFT reference number for this journal entry.',
    )
    x_bank_journal_id = fields.Many2one(
        'account.journal',
        string='Bank',
        domain="[('type', 'in', ('bank', 'cash'))]",
        tracking=True,
        help='Bank / cash journal for this journal entry payment. '
             'Determines which cheque series is available in the dropdown below.',
    )
    x_cheque_leaf_id = fields.Many2one(
        'x.cheque.leaf',
        string='Cheque No. (Series)',
        domain="[('bank_journal_id', '=', x_bank_journal_id), ('state', '=', 'available')]",
        ondelete='set null',
        tracking=True,
        help='Select from active cheque series for the chosen bank. '
             'Auto-fills Cheque No. and marks the leaf as used on posting.',
    )
    x_account_title = fields.Char(
        string='Account Title',
        help='Bank account title / beneficiary name for this journal entry payment.',
    )

    # ── Finance HO journal entry: Source / Destination project ──────────────
    # When Finance HO posts a payment via a manual journal entry (instead of
    # the Vendor Payment wizard), these two fields capture which project's
    # funds are going out (source / credit side) and which site project's
    # payable is being settled (destination / debit side).
    #
    # Auto-fill logic:
    #   credit lines (bank going down)   → source project analytic
    #   debit lines  (payable settled)   → destination project analytic
    #
    # x_project_analytic_account_id is also set = destination project so that
    # existing GL / dashboard queries that filter on the move header field
    # continue to work without modification.
    x_ho_source_project_id = fields.Many2one(
        'account.analytic.account',
        string='Source Project',
        tracking=True,
        help='HO payment (JE): project whose funds cover this payment — '
             'credit-side lines (bank going down) receive this analytic. '
             'Finance HO only, journal entries only.',
    )
    x_ho_dest_project_id = fields.Many2one(
        'account.analytic.account',
        string='Destination Project',
        tracking=True,
        help='HO payment (JE): site project whose vendor payable is being settled — '
             'debit-side lines (payable going down) receive this analytic. '
             'Also synced to x_project_analytic_account_id for dashboard queries. '
             'Finance HO only, journal entries only.',
    )

    @api.onchange('x_ho_source_project_id', 'x_ho_dest_project_id')
    def _onchange_ho_payment_projects_fill_lines(self):
        """Finance HO journal entry payment: fill analytic on ALL existing lines.

        Rule:
          credit lines (bank / fund going down)     → source project analytic
          debit  lines (payable / expense settled)  → destination project analytic

        Lines with no debit or credit amount yet are left alone — they will be
        filled by _onchange_line_ids_fill_analytic_for_entry when amounts are set.

        Also mirrors the destination to x_project_analytic_account_id so that
        existing GL / dashboard queries (which read the move-header field) pick
        up this JE for the destination site project automatically.
        """
        if self.move_type != 'entry':
            return
        if not self.env.user.has_group('site_operations.group_finance_ho'):
            return

        source = self.x_ho_source_project_id
        dest = self.x_ho_dest_project_id

        # Keep move-header analytic in sync with destination for existing queries.
        if dest:
            self.x_project_analytic_account_id = dest
        elif source:
            self.x_project_analytic_account_id = source

        source_dist = {str(source.id): 100.0} if source else {}
        dest_dist = {str(dest.id): 100.0} if dest else {}

        for line in self.line_ids:
            if line.credit > 0 and source_dist:
                line.analytic_distribution = source_dist
            elif line.debit > 0 and dest_dist:
                line.analytic_distribution = dest_dist

    @api.onchange('x_bank_journal_id')
    def _onchange_je_bank_journal(self):
        """Clear cheque leaf when bank journal changes — leaf is bank-specific."""
        if self.x_cheque_leaf_id and (
            self.x_cheque_leaf_id.bank_journal_id != self.x_bank_journal_id
        ):
            leaf = self.x_cheque_leaf_id
            self.x_cheque_leaf_id = False
            self.x_cheque_number = False
            leaf.sudo().write({'state': 'available'})

    @api.onchange('x_cheque_leaf_id')
    def _onchange_je_cheque_leaf(self):
        if self.x_cheque_leaf_id:
            self.x_cheque_number = self.x_cheque_leaf_id.cheque_number

    @api.onchange('journal_id')
    def _onchange_je_journal_clear_leaf(self):
        """When main journal changes to a bank type, sync x_bank_journal_id."""
        if self.journal_id and self.journal_id.type in ('bank', 'cash'):
            if not self.x_bank_journal_id:
                self.x_bank_journal_id = self.journal_id
        # Clear leaf if bank no longer matches
        if self.x_cheque_leaf_id and (
            self.x_cheque_leaf_id.bank_journal_id != self.x_bank_journal_id
        ):
            leaf = self.x_cheque_leaf_id
            self.x_cheque_leaf_id = False
            self.x_cheque_number = False
            leaf.sudo().write({'state': 'available'})

    def action_discard_je_cheque_leaf(self):
        """Discard the assigned cheque leaf — marks it unusable and clears it from the JE."""
        self.ensure_one()
        if self.x_cheque_leaf_id:
            self.x_cheque_leaf_id.sudo().action_discard()
            self.x_cheque_leaf_id = False

    def write(self, vals):
        res = super().write(vals)
        # Propagate cheque number / account title from the move header down to ALL
        # its journal lines that do not yet have their own per-line value.
        # This covers:
        #   • Manual journal entries typed by the user on the JE form header
        #   • Single-bank payments (cheque propagated from payment → move → lines)
        # Multi-bank allocation payments stamp each line directly in
        # _prepare_move_line_default_vals (payment_allocation logic) and are NOT
        # affected here because their lines already carry per-line values.
        for field in ('x_cheque_number', 'x_account_title'):
            if field in vals and vals[field]:
                for move in self:
                    blank_lines = move.line_ids.filtered(lambda l: not l[field])
                    if blank_lines:
                        blank_lines.write({field: vals[field]})
        return res

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
        """For journal entries (move_type='entry'): auto-fill analytic on new lines.

        Site Accountant path:
          All lines get the same single project analytic from
          x_project_analytic_account_id (the user's site default).

        Finance HO path:
          Credit lines (bank going down) → x_ho_source_project_id analytic.
          Debit  lines (payable settled) → x_ho_dest_project_id analytic.
          Lines with zero debit and credit are skipped until an amount is entered.

        invoice_line_ids does not apply to MISC entries — we use line_ids
        directly here.  The DB-level create() hook below handles the same
        for server-side and programmatic record creation."""
        if self.move_type != 'entry':
            return

        is_sa = self.env.user.has_group('site_operations.group_site_accountant')
        is_fo = self.env.user.has_group('site_operations.group_finance_ho')

        if is_sa:
            if not self.x_project_analytic_account_id:
                return
            dist = {str(self.x_project_analytic_account_id.id): 100.0}
            for line in self.line_ids:
                if not line.analytic_distribution:
                    line.analytic_distribution = dist

        elif is_fo:
            source = self.x_ho_source_project_id
            dest = self.x_ho_dest_project_id
            if not source and not dest:
                return
            source_dist = {str(source.id): 100.0} if source else {}
            dest_dist = {str(dest.id): 100.0} if dest else {}
            for line in self.line_ids:
                if line.analytic_distribution:
                    continue  # already tagged — don't overwrite
                if line.credit > 0 and source_dist:
                    line.analytic_distribution = source_dist
                elif line.debit > 0 and dest_dist:
                    line.analytic_distribution = dest_dist
                # zero-amount lines left alone; they'll be filled on next change

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
        # ── 1. Identify pre-balanced invoices BEFORE any writes ──────────────────
        # When a site accountant manually enters ALL lines on a vendor bill or
        # customer invoice — including one or more payable/receivable accounts
        # on both sides (e.g. backcharge pattern: Dr Expense / Cr Payable NAEEM,
        # Dr Payable SHAHZAD / Cr Expense) — the entry is already balanced.
        #
        # Odoo's payment_term sync (_sync_dynamic_lines) fires during write() and
        # tries to delete/recreate the auto-managed payable line.  This causes:
        #   • "You cannot delete a payable/receivable line" — if it tries to remove
        #     the existing payment_term line while recreating it
        #   • "The entry is not balanced" — if it adds an EXTRA payable line on
        #     top of the manually-added ones, doubling the credit
        #
        # The fix: for pre-balanced invoices that have manually-added payable/
        # receivable lines (display_type != 'payment_term'), post using
        # _post(soft=False) with skip_invoice_sync=True.  This bypasses the
        # payment_term sync entirely, preserving the manually-balanced lines.
        def _has_manual_payable(move):
            return any(
                l.account_id.account_type in ('liability_payable', 'asset_receivable')
                and l.display_type != 'payment_term'
                for l in move.line_ids
            )

        pre_balanced = self.env['account.move']
        for move in self.filtered(
            lambda m: m.is_invoice(include_receipts=True) and m.state != 'posted'
        ):
            dr = sum(move.line_ids.mapped('debit'))
            cr = sum(move.line_ids.mapped('credit'))
            if abs(dr - cr) < 0.01 and dr > 0.01 and _has_manual_payable(move):
                pre_balanced |= move

        normal_moves = self - pre_balanced

        # ── 2. invoice_date_due pre-fill (normal moves only) ─────────────────────
        # Due dates are hidden in the UI.  Ensure the field is populated before
        # posting so Odoo can set date_maturity on the payment_term line correctly.
        # Skip pre_balanced moves — their payment_term line is NOT touched by the
        # sync, so stamping invoice_date_due would needlessly trigger the sync.
        for move in normal_moves.filtered(lambda m: m.is_invoice() and not m.invoice_date_due):
            move.invoice_date_due = move.invoice_date or fields.Date.context_today(self)

        # For customer invoices: also stamp date_maturity on any existing
        # payment_term lines that still have no due date.
        for move in normal_moves.filtered(lambda m: m.move_type == 'out_invoice'):
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

        # ── 3. Bill Copy mandatory for vendor bills ───────────────────────────────
        # Skip during module installation / demo-data loading.
        if not self.env.registry._init and not self.env.context.get('install_mode'):
            missing_attachment = self.filtered(
                lambda m: (
                    m.move_type == 'in_invoice'
                    and m.state != 'posted'
                    and not m.x_source_picking_id
                    and not m.x_bill_copy
                )
            )
        else:
            missing_attachment = self.browse()
        if missing_attachment:
            names = ', '.join(m.name or _('New') for m in missing_attachment)
            raise UserError(_(
                'Bill Copy attachment is mandatory before posting a Vendor Bill.\n\n'
                'Please upload the physical bill document for: %s'
            ) % names)

        # ── 4. HO/Finance JE validation: payable lines must carry an analytic ────
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

        # ── 5. Post pre-balanced invoices bypassing invoice sync ─────────────────
        # skip_invoice_sync=True causes _sync_dynamic_lines to yield immediately
        # (disabled=True via _disable_recursion), so the payment_term sync never
        # runs and the manually-balanced lines are preserved as-is.
        is_sa = self.env.user.has_group('site_operations.group_site_accountant')
        if pre_balanced:
            target = pre_balanced.sudo() if is_sa else pre_balanced
            target.with_context(skip_invoice_sync=True)._post(soft=False)

        # ── 6. Post normal moves through the standard Odoo flow ──────────────────
        # Site accountants need sudo() because group_account_readonly ≠
        # group_account_invoice (mutually exclusive Odoo 19 privilege levels).
        if normal_moves:
            if is_sa:
                super(AccountMoveSiteOps, normal_moves.sudo()).action_post()
            else:
                super(AccountMoveSiteOps, normal_moves).action_post()

        # ── 7. Post-processing for ALL posted moves (pre_balanced + normal) ──────
        for move in self.filtered(
            lambda m: m.move_type == 'in_invoice' and m.state == 'posted'
        ):
            # Close "Review vendor bill" activity (auto-created on stock receipt).
            matracon_notify.close_activities(move, summary_contains='Review vendor bill')
            # Apply project analytic AFTER posting so Odoo's line recompute
            # (tax lines, payment_term lines) cannot overwrite the distribution.
            if not move.x_source_picking_id:
                # Last-chance analytic auto-fill (Excel import / no-default-user).
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

        # Recompute project billed amount for any customer invoices just posted.
        self._matracon_update_project_billed_amount()

        # Auto-sync liability sheets for journal entries with payable lines.
        for move in self.filtered(
            lambda m: m.move_type == 'entry'
            and m.state == 'posted'
            and not m.x_source_picking_id
        ):
            move._ensure_liability_from_journal_entry()

        # Mark cheque leaves as used for posted journal entries.
        for move in self.filtered(
            lambda m: m.move_type == 'entry'
            and m.state == 'posted'
            and m.x_cheque_leaf_id
            and m.x_cheque_leaf_id.state == 'available'
        ):
            move.x_cheque_leaf_id.sudo().write({'state': 'used'})

        return False

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
        # Release cheque leaves back to available when JE is reset to draft
        for move in self.filtered(
            lambda m: m.move_type == 'entry'
            and m.x_cheque_leaf_id
            and m.x_cheque_leaf_id.state == 'used'
        ):
            move.x_cheque_leaf_id.sudo().write({'state': 'available'})
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

    @api.model
    def _register_hook(self):
        self.env.cr.execute("""
            ALTER TABLE account_move_line
                ADD COLUMN IF NOT EXISTS x_cheque_number VARCHAR,
                ADD COLUMN IF NOT EXISTS x_account_title VARCHAR,
                ADD COLUMN IF NOT EXISTS x_cheque_leaf_id INTEGER
        """)
        return super()._register_hook()

    # Standalone stored fields (NOT related) so each move line can hold its own
    # cheque number — essential for multi-bank allocation payments where each
    # bank split line carries a different cheque number.
    # Propagation from move → lines is handled by AccountMoveSiteOps.write().
    x_cheque_number = fields.Char(
        string='Cheque / Ref No.',
        store=True,
        copy=False,
    )
    x_account_title = fields.Char(
        string='Account Title',
        store=True,
        copy=False,
    )
    x_cheque_leaf_id = fields.Many2one(
        'x.cheque.leaf',
        string='Cheque No. (Series)',
        # Domain kept broad at model level; view narrows it by bank journal.
        # '|' lets the dropdown show leaves from either the explicit header bank
        # (x_bank_journal_id) or the entry's own journal (when the journal IS a
        # bank journal, e.g. BankIslami).  Both are tested against the leaf's
        # stored bank_journal_id.  If neither is a bank journal the filter still
        # returns nothing — which is correct (no cheques for MISC entries).
        domain="[('state', '=', 'available'), '|', ('bank_journal_id', '=', parent.x_bank_journal_id), ('bank_journal_id', '=', parent.journal_id)]",
        ondelete='set null',
        store=True,
        copy=False,
        help='Select from the active cheque series filtered by the journal entry bank.',
    )

    @api.onchange('x_cheque_leaf_id')
    def _onchange_line_cheque_leaf(self):
        if self.x_cheque_leaf_id:
            self.x_cheque_number = self.x_cheque_leaf_id.cheque_number

    def action_discard_je_line_cheque_leaf(self):
        """Discard the cheque leaf assigned to this journal entry line.

        Marks the leaf as discarded (spoiled/faulty), clears the reference
        on the line, and returns the leaf to the series as unusable.
        Mirrors the same action on x.payment.bank.allocation.
        """
        self.ensure_one()
        if not self.x_cheque_leaf_id:
            raise UserError(_('No cheque assigned to this line — nothing to discard.'))
        leaf = self.x_cheque_leaf_id
        self.write({
            'x_cheque_leaf_id': False,
            'x_cheque_number': False,
        })
        leaf.sudo().write({
            'state': 'discarded',
            'discarded_date': fields.Date.today(),
        })
        return False

    def _fix_direct_debit_credit(self, vals):
        """If the caller supplied debit/credit directly (without price_unit) on
        an invoice line that has no product, back-fill price_unit so that
        Odoo's _compute_totals → _sync_dynamic_lines chain produces the correct
        price_subtotal and therefore the correct AP/AR counterpart balance.

        Without this, price_unit stays 0 → price_subtotal = 0 → needed_terms = 0
        → the AP line carries 0 → the entry stores balance = 0 for the user line.
        """
        debit = vals.get('debit') or 0
        credit = vals.get('credit') or 0
        if (debit or credit) and not vals.get('product_id') and 'price_unit' not in vals:
            vals['price_unit'] = debit - credit        # sign preserved
            vals.setdefault('quantity', 1.0)

    @api.model_create_multi
    def create(self, vals_list):
        # Cache move objects to avoid repeated browses for the same move.
        move_cache = {}
        for vals in vals_list:
            # ── Fix 1: back-fill price_unit from debit/credit so invoice line
            #    amounts are not zeroed by _compute_totals.
            move_id = vals.get('move_id')
            if move_id:
                if move_id not in move_cache:
                    move_cache[move_id] = self.env['account.move'].browse(move_id)
                if move_cache[move_id].is_invoice(include_receipts=True):
                    self._fix_direct_debit_credit(vals)

            # ── Fix 2: auto-fill analytic_distribution on draft invoice / entry lines.
            if vals.get('analytic_distribution'):
                continue  # already set — respect explicit caller value
            if not move_id:
                continue
            move = move_cache.setdefault(move_id, self.env['account.move'].browse(move_id))
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

        records = super().create(vals_list)

        # ── Fix 3: site cash-account fallback for entry lines still missing analytic.
        #
        # Covers the case where an admin (not a site accountant) creates a journal
        # entry — e.g. an Opening Balance entry — that includes a line on a site's
        # petty cash account.  Fix 2 above skips that path (it requires is_sa_entry).
        # Here we look up the site's analytic from the account itself so the OB
        # and similar entries appear in the site-filtered General Ledger.
        lines_no_analytic = records.filtered(
            lambda l: l.move_id.move_type == 'entry' and not l.analytic_distribution
        )
        if lines_no_analytic:
            site_configs = self.env['project.site.config'].sudo().search([
                ('x_petty_cash_account_id', '!=', False),
                ('analytic_account_id', '!=', False),
            ])
            cash_to_analytic = {
                sc.x_petty_cash_account_id.id: sc.analytic_account_id
                for sc in site_configs
            }
            for line in lines_no_analytic:
                analytic = cash_to_analytic.get(line.account_id.id)
                if analytic:
                    line.sudo().write(
                        {'analytic_distribution': {str(analytic.id): 100.0}}
                    )
                    # Also tag the move header so record rules let site SAs see it.
                    if not line.move_id.x_project_analytic_account_id:
                        line.move_id.sudo().write(
                            {'x_project_analytic_account_id': analytic.id}
                        )

        return records

    def write(self, vals):
        """When the user edits debit/credit directly on an existing invoice line
        (through the Journal Items tab), keep price_unit in sync so
        _compute_totals → needed_terms produces the right AP/AR balance.
        """
        if ('debit' in vals or 'credit' in vals) and 'price_unit' not in vals:
            # Identify lines that have no product and are on invoices
            invoice_lines = self.filtered(
                lambda l: not l.product_id and l.move_id.is_invoice(include_receipts=True)
            )
            if invoice_lines:
                debit = vals.get('debit') or 0
                credit = vals.get('credit') or 0
                balance = debit - credit
                # Write with price_unit for the no-product invoice lines
                super(AccountMoveLineSiteOps, invoice_lines).write(
                    dict(vals, price_unit=balance, quantity=vals.get('quantity') or 1.0)
                )
                other_lines = self - invoice_lines
                if other_lines:
                    return super(AccountMoveLineSiteOps, other_lines).write(vals)
                return True
        return super().write(vals)
