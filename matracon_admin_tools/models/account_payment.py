import logging

from odoo import api, models, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class AccountPayment(models.Model):
    """action_fix_payment_analytic relocated, unchanged, from the retired
    x.petty.cash.admin.wizard (site_operations/models/petty_cash.py) -
    that wizard had no input fields of its own, so retiring it in favour of
    a direct Server Action (see data/fix_tools_data.xml) loses nothing.
    """
    _inherit = 'account.payment'

    @api.model
    def action_fix_payment_analytic(self):
        """Retroactively stamp analytic_distribution on all payment JE lines.

        Problem fixed:
          When Finance HO releases petty cash (or makes any payment tagged to a
          project), the JE lines that hit site accounts (e.g. "112632 Cash at
          RWASA") were missing analytic_distribution because the old code only
          stamped it on liability_payable/expense/asset_receivable types.
          Cash-type accounts (asset_cash) were skipped.

          Result: site accountants applying the analytic filter in their GL
          saw ZERO in the Debit column for their cash account - only the
          credits (their own expense entries) showed, making the balance look
          like a massive overdraft.

        What this does:
          For every posted account.payment linked to a project
          (x_destination_project_id or x_fund_project_id):
          1. Ensures x_project_analytic_account_id is set on the move header.
          2. Identifies the HO source-bank lines (journal default_account_id /
             outstanding_account_id / multi-bank allocation accounts) - these
             must NOT get the site's analytic (they belong to HO).
          3. Stamps analytic_distribution on every OTHER line that is missing it.

        Safe to run multiple times - only touches lines where analytic is blank.
        Each site's analytic is scoped correctly: each SA sees only their own.
        """
        if not (self.env.user.has_group('purchase_demand_raise.group_matracon_admin')
                or self.env.user.has_group('base.group_system')):
            raise UserError(_('Only Matracon Admin or System Administrator can run this action.'))

        self.env.cr.execute("""
            SELECT id FROM account_payment
            WHERE (x_destination_project_id IS NOT NULL
                   OR x_fund_project_id IS NOT NULL)
              AND state NOT IN ('draft', 'cancel')
              AND move_id IS NOT NULL
        """)
        payment_ids = [r[0] for r in self.env.cr.fetchall()]
        payments = self.env['account.payment'].sudo().browse(payment_ids)

        fixed_lines = 0
        fixed_moves = 0

        for payment in payments:
            analytic = payment.x_destination_project_id or payment.x_fund_project_id
            if not analytic or not payment.move_id:
                continue

            # Ensure move header is tagged (drives the security record rule)
            if not payment.move_id.x_project_analytic_account_id:
                payment.move_id.sudo().write(
                    {'x_project_analytic_account_id': analytic.id}
                )

            # Collect HO source-bank account IDs to exclude
            bank_acct_ids = set()
            if payment.outstanding_account_id:
                bank_acct_ids.add(payment.outstanding_account_id.id)
            if payment.journal_id and payment.journal_id.default_account_id:
                bank_acct_ids.add(payment.journal_id.default_account_id.id)
            for alloc in payment.x_bank_allocation_ids:
                if alloc.journal_id and alloc.journal_id.default_account_id:
                    bank_acct_ids.add(alloc.journal_id.default_account_id.id)

            dist = {str(analytic.id): 100.0}
            lines_to_fix = payment.move_id.line_ids.filtered(
                lambda l: l.account_id.id not in bank_acct_ids
                    and not l.analytic_distribution
            )
            if lines_to_fix:
                lines_to_fix.sudo().write({'analytic_distribution': dist})
                fixed_lines += len(lines_to_fix)
                fixed_moves += 1
                _logger.info(
                    'fix_payment_analytic: %s (%d) → analytic %s stamped on %d lines',
                    payment.name, payment.id, analytic.name, len(lines_to_fix),
                )

        # ── Pass 2: MISC journal entries with site cash-account lines ──────────────
        # Covers Opening Balance entries and any other posted MISC entries (not
        # linked to account.payment) that have lines on a site's petty cash account
        # but are missing analytic_distribution.
        #
        # Mapping: project.site.config.x_petty_cash_account_id → analytic_account_id
        site_configs = self.env['x.project.site.config'].sudo().search([
            ('x_petty_cash_account_id', '!=', False),
            ('analytic_account_id', '!=', False),
        ])
        cash_to_analytic = {
            sc.x_petty_cash_account_id.id: sc.analytic_account_id
            for sc in site_configs
        }
        if cash_to_analytic:
            account_ids = list(cash_to_analytic.keys())
            # Find posted MISC entries that:
            #   - have a line on a site cash account with no analytic, AND
            #   - are NOT the JE of an account.payment (those were fixed in Pass 1)
            self.env.cr.execute("""
                SELECT DISTINCT am.id, aml.account_id
                FROM account_move am
                JOIN account_move_line aml ON aml.move_id = am.id
                WHERE am.move_type = 'entry'
                  AND am.state = 'posted'
                  AND aml.account_id = ANY(%s)
                  AND (aml.analytic_distribution IS NULL
                       OR aml.analytic_distribution::text = '{}')
                  AND NOT EXISTS (
                      SELECT 1 FROM account_payment ap WHERE ap.move_id = am.id
                  )
            """, (account_ids,))
            rows = self.env.cr.fetchall()

            # Build move_id → analytic mapping (first site cash account wins)
            move_to_analytic = {}
            for move_id, account_id in rows:
                if move_id not in move_to_analytic:
                    analytic = cash_to_analytic.get(account_id)
                    if analytic:
                        move_to_analytic[move_id] = analytic

            entries = self.env['account.move'].sudo().browse(list(move_to_analytic.keys()))
            for entry in entries:
                analytic = move_to_analytic[entry.id]
                dist = {str(analytic.id): 100.0}

                # Stamp the move header if blank (drives site-SA record rules)
                if not entry.x_project_analytic_account_id:
                    entry.sudo().write(
                        {'x_project_analytic_account_id': analytic.id}
                    )

                # Stamp every line that is missing analytic
                lines_missing = entry.line_ids.filtered(
                    lambda l: not l.analytic_distribution
                )
                if lines_missing:
                    lines_missing.sudo().write({'analytic_distribution': dist})
                    fixed_lines += len(lines_missing)
                    fixed_moves += 1
                    _logger.info(
                        'fix_payment_analytic: MISC entry %s (%d) → analytic %s'
                        ' stamped on %d lines',
                        entry.name, entry.id, analytic.name, len(lines_missing),
                    )

        _logger.info(
            'fix_payment_analytic: complete — %d lines fixed across %d entries'
            ' (payments + MISC)',
            fixed_lines, fixed_moves,
        )
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('GL Analytic Fix Complete'),
                'message': _(
                    'Stamped analytic distribution on %(lines)d journal entry lines '
                    'across %(moves)d entries (payment JEs + Opening Balance / MISC). '
                    'Site accountants can now see all HO-created entries in their '
                    'filtered General Ledger. Check the server log for details.'
                ) % {'lines': fixed_lines, 'moves': fixed_moves},
                'type': 'success',
                'sticky': True,
            },
        }
