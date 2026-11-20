"""
Migration 1.8.0 — Set Pakistan fiscal year (July 1 – June 30) on all companies.

Odoo's accounting reports and date-range pickers derive their "Year" period from:
    fiscalyear_last_day   = 30   (June 30)
    fiscalyear_last_month = '6'  (June)

Uses raw SQL so it cannot be blocked by noupdate flags or ORM validation.
"""
import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    cr.execute(
        """
        UPDATE res_company
        SET    fiscalyear_last_day   = 30,
               fiscalyear_last_month = '6'
        WHERE  fiscalyear_last_month != '6'
           OR  fiscalyear_last_day   != 30
        """
    )
    if cr.rowcount:
        _logger.info(
            'migration 1.8.0: set %d company record(s) to Pakistan fiscal year (Jul–Jun)',
            cr.rowcount,
        )
    else:
        _logger.info('migration 1.8.0: fiscal year already set to Jul–Jun — no changes')
