from dateutil.relativedelta import relativedelta
from markupsafe import Markup

from odoo import models, fields, api, _


class AccountMoveSiteOps(models.Model):
    """Extend account.move (vendor bills) to auto-update project liability sheets.

    When a site accountant posts a vendor bill that carries a project analytic
    account, the bill's total is added to the *New Liability (Bills)* column of
    the matching draft liability sheet — exactly the same way material issuance
    backcharges are accumulated.  Resetting to draft reverses the registration.
    """
    _inherit = 'account.move'

    # ── Schema guard ─────────────────────────────────────────────────────────
    @api.model
    def _register_hook(self):
        self.env.cr.execute("""
            ALTER TABLE account_move
                ADD COLUMN IF NOT EXISTS x_project_analytic_account_id  INTEGER,
                ADD COLUMN IF NOT EXISTS x_liability_registered          BOOLEAN
                    NOT NULL DEFAULT FALSE,
                ADD COLUMN IF NOT EXISTS x_liability_amount_registered   DOUBLE PRECISION
                    NOT NULL DEFAULT 0.0
        """)
        return super()._register_hook()

    # ── Fields ───────────────────────────────────────────────────────────────
    x_project_analytic_account_id = fields.Many2one(
        'account.analytic.account',
        string='Project (Site)',
        tracking=True,
        help='Project this vendor bill belongs to — used to update the '
             'project liability sheet automatically on posting.',
    )

    x_liability_registered = fields.Boolean(
        string='Liability Registered',
        default=False, readonly=True, copy=False,
        help='True once this bill has updated a liability sheet line.')

    x_liability_amount_registered = fields.Float(
        string='Liability Amount Registered',
        default=0.0, readonly=True, copy=False,
        help='Exact amount that was added to the liability sheet (used for reversal).')

    # ── Hooks ─────────────────────────────────────────────────────────────────

    def action_post(self):
        res = super().action_post()
        for move in self.filtered(
            lambda m:
                m.move_type == 'in_invoice'
                and m.state == 'posted'
                and not m.x_liability_registered
                and m.x_project_analytic_account_id
                and m.partner_id
        ):
            move._update_liability_sheet_from_bill()
        return res

    def button_draft(self):
        for move in self.filtered(
            lambda m: m.move_type == 'in_invoice' and m.x_liability_registered
        ):
            move._reverse_liability_sheet_from_bill()
        return super().button_draft()

    # ── Core helpers ─────────────────────────────────────────────────────────

    def _update_liability_sheet_from_bill(self):
        """Add this bill's total to the project liability sheet.

        Finds (or creates) the *draft* liability sheet for the bill's project
        and month, then accumulates the amount on the vendor's line in the
        ``new_liability`` column.
        """
        self.ensure_one()
        amount = self.amount_total
        if not amount:
            return

        bill_date = self.invoice_date or fields.Date.today()
        month_start = bill_date.replace(day=1)
        month_end = (month_start + relativedelta(months=1)) - relativedelta(days=1)

        LiabilitySheet = self.env['x.liability.sheet'].sudo()

        # Reuse an existing draft sheet for the same project + month
        sheet = LiabilitySheet.search([
            ('project_analytic_account_id', '=', self.x_project_analytic_account_id.id),
            ('date_from', '=', month_start),
            ('state', '=', 'draft'),
        ], limit=1)

        if not sheet:
            sheet = LiabilitySheet.create({
                'project_analytic_account_id': self.x_project_analytic_account_id.id,
                'date_from': month_start,
                'date_to': month_end,
            })
            self.message_post(
                body=Markup(_('Liability Sheet <b>%s</b> auto-created for project <b>%s</b>.')) % (
                    sheet.name,
                    self.x_project_analytic_account_id.name,
                )
            )

        # Accumulate on the existing vendor line; create one if absent
        existing_line = sheet.line_ids.filtered(
            lambda l: l.partner_id.id == self.partner_id.id
        )
        if existing_line:
            line = existing_line[0]
            line.write({'new_liability': line.new_liability + amount})
        else:
            desc = (
                self.ref
                or self.name
                or _('Vendor Bill — %s') % self.partner_id.name
            )
            sheet.write({
                'line_ids': [(0, 0, {
                    'description': desc,
                    'partner_id': self.partner_id.id,
                    'new_liability': amount,
                })]
            })

        # Remember what we registered so we can reverse it exactly if needed
        self.write({
            'x_liability_registered': True,
            'x_liability_amount_registered': amount,
        })
        self.message_post(
            body=Markup(_(
                'Liability Sheet <b>%(sheet)s</b> updated — vendor <b>%(vendor)s</b>: '
                '<b>+%(amount)s</b> added to <i>New Liability (Bills)</i>.'
            )) % {
                'sheet': sheet.name,
                'vendor': self.partner_id.name,
                'amount': f'{amount:,.2f}',
            }
        )

    def _reverse_liability_sheet_from_bill(self):
        """Remove the previously registered amount from the liability sheet.

        Called when the bill is reset to draft so the liability sheet stays
        in sync.  Searches draft/submitted sheets (not yet CEO-approved) so
        we don't accidentally modify locked entries.
        """
        self.ensure_one()
        if not self.x_liability_registered or not self.x_liability_amount_registered:
            return

        amount = self.x_liability_amount_registered
        LiabilitySheet = self.env['x.liability.sheet'].sudo()

        sheets = LiabilitySheet.search([
            ('project_analytic_account_id', '=', self.x_project_analytic_account_id.id),
            ('state', 'in', ['draft', 'submitted']),
        ])
        for sheet in sheets:
            for line in sheet.line_ids:
                if line.partner_id == self.partner_id:
                    line.write({
                        'new_liability': max(line.new_liability - amount, 0.0),
                    })
                    self.write({
                        'x_liability_registered': False,
                        'x_liability_amount_registered': 0.0,
                    })
                    self.message_post(
                        body=Markup(_(
                            'Liability Sheet <b>%(sheet)s</b> reversed — vendor <b>%(vendor)s</b>: '
                            '<b>−%(amount)s</b> removed from <i>New Liability (Bills)</i> '
                            '(bill reset to draft).'
                        )) % {
                            'sheet': sheet.name,
                            'vendor': self.partner_id.name,
                            'amount': f'{amount:,.2f}',
                        }
                    )
                    return

        # Sheet line not found (may have been manually edited) — just clear the flag
        self.write({
            'x_liability_registered': False,
            'x_liability_amount_registered': 0.0,
        })
        self.message_post(
            body=Markup(_(
                'Bill reset to draft — no matching liability sheet line found for '
                '<b>%s</b>; reversal skipped (line may have been manually removed).'
            )) % self.partner_id.name
        )
