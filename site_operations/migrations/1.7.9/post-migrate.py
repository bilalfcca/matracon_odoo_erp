"""
Migration 1.7.9 — Set PKR (Pakistani Rupee) to 0 decimal places.
PKR paise are not used in practice; all amounts display as whole numbers.
The base.PKR record has noupdate=True so it cannot be changed from a data file.
"""
import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    cr.execute(
        "UPDATE res_currency SET decimal_places = 0 WHERE name = 'PKR' AND decimal_places != 0"
    )
    if cr.rowcount:
        _logger.info('migration 1.7.9: set PKR to 0 decimal places (%d row)', cr.rowcount)
