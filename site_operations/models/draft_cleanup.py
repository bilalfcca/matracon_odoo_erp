"""Daily Draft Cleanup — deletes accounting-related draft records
older than 7 days.

Running daily ensures every record survives at least 7 full days from its
creation date.  A weekly Sunday cron would delete records on Sunday regardless
of which day they were created — a record created on Monday would be checked
the following Sunday (6-13 days later) and a record created the same Sunday
could be only hours old at midnight.  Daily cadence removes that uncertainty:
each record is deleted within 24 h of its 7-day birthday.

Excluded by design:
  • purchase.requisition / purchase.order  — procurement workflow
  • x.liability.sheet                      — explicitly excluded
  • x.comparative.statement                — procurement, not accounting
  • stock.picking                           — operational
  • account.payment where x_ceo_approval_state = 'submitted'
                                           — already sent to CEO; keep it

Runs every night at 00:00 via ir.cron defined in
data/draft_cleanup_cron.xml.
"""

import logging
from datetime import timedelta
from odoo import models, api, fields

_logger = logging.getLogger(__name__)

# Records must be at least this many days old before they are deleted.
_DRAFT_AGE_DAYS = 7


class DraftCleanup(models.AbstractModel):
    _name = 'x.draft.cleanup'
    _description = 'Accounting Draft Cleanup (daily cron)'

    # ─────────────────────────────────────────────────────────────────────────
    # CRON ENTRY POINT
    # ─────────────────────────────────────────────────────────────────────────

    @api.model
    def action_cleanup_drafts(self):
        """Delete accounting-related draft records older than 7 days.

        Called by the daily ir.cron at 00:00.  Running daily guarantees
        every record survives at least 7 full days from creation.  Each
        model is processed independently so a failure on one model does
        not abort the rest.
        """
        cutoff = fields.Datetime.now() - timedelta(days=_DRAFT_AGE_DAYS)
        results = {}

        # ── helpers ────────────────────────────────────────────────────────

        def _delete(model, domain_extra=None):
            """Search draft records older than the cutoff and unlink them.

            Returns the count of records deleted.  If the model is not
            registered in the current database (e.g. Studio models absent
            from this environment) the call is silently skipped.
            """
            if model not in self.env:
                _logger.warning(
                    'Draft cleanup: model %s not in registry, skipping', model
                )
                return 0
            domain = [
                ('state', '=', 'draft'),
                ('create_date', '<', cutoff),
            ]
            if domain_extra:
                domain += domain_extra
            try:
                recs = self.env[model].search(domain)
                count = len(recs)
                if recs:
                    recs.unlink()
                return count
            except Exception:
                _logger.exception('Draft cleanup: error unlinking %s', model)
                return 0

        # ── 1. Journal Entries / Vendor Bills / Customer Invoices ───────────
        results['account.move'] = _delete('account.move')

        # ── 2. Payments — keep those already submitted to CEO ───────────────
        results['account.payment'] = _delete(
            'account.payment',
            [('x_ceo_approval_state', '!=', 'submitted')],
        )

        # ── 3. Batch payments ────────────────────────────────────────────────
        results['x.batch.payment'] = _delete('x.batch.payment')

        # ── 4. Petty cash requests (cascades to child expense lines) ─────────
        results['x.petty.cash.request'] = _delete('x.petty.cash.request')

        # ── 5. Draft petty cash expense lines ───────────────────────────────
        results['x.petty.cash.expense'] = _delete('x.petty.cash.expense')

        # ── 6. IPC certificates ──────────────────────────────────────────────
        results['x.subcontractor.ipc'] = _delete('x.subcontractor.ipc')

        # ── 7. Bank guarantees ───────────────────────────────────────────────
        results['x.bank.guarantee'] = _delete('x.bank.guarantee')

        # ── 8. Tax notices  (model _name = 'x.tax.notice.order') ────────────
        results['x.tax.notice.order'] = _delete('x.tax.notice.order')

        # ── 9. WHT certificates ──────────────────────────────────────────────
        results['x.wht.certificate'] = _delete('x.wht.certificate')

        # ── 10. Salary sheets ────────────────────────────────────────────────
        results['x.salary.sheet'] = _delete('x.salary.sheet')

        # ── 11. Attendance sheets ────────────────────────────────────────────
        results['x.attendance.sheet'] = _delete('x.attendance.sheet')

        # ── Summary log ─────────────────────────────────────────────────────
        total = sum(results.values())
        detail = ', '.join(
            f'{model}={count}'
            for model, count in results.items()
            if count
        )
        _logger.info(
            'Draft cleanup complete (cutoff: %s, age >= %d days) — '
            '%d records deleted. Breakdown: %s',
            cutoff.strftime('%Y-%m-%d %H:%M'),
            _DRAFT_AGE_DAYS,
            total,
            detail or 'nothing to delete',
        )
