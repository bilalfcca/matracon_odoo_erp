"""Migration 19.0.2.1.6 — Restore CoA hierarchy (x_parent_account_id / x_is_main).

Runs setup_main_accounts_and_bank_children then setup_account_groups so that
existing production databases get the full hierarchy applied on the first
upgrade after these features are re-introduced.
"""
import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    from odoo import api
    from odoo.addons.site_operations.hooks import (
        setup_main_accounts_and_bank_children,
        setup_account_groups,
    )

    env = api.Environment(cr, 1, {})

    try:
        setup_main_accounts_and_bank_children(env)
    except Exception as e:
        _logger.warning('2.1.6 migrate: setup_main_accounts_and_bank_children failed: %s', e)

    try:
        setup_account_groups(env)
    except Exception as e:
        _logger.warning('2.1.6 migrate: setup_account_groups failed: %s', e)
