import logging

from odoo import models

_logger = logging.getLogger(__name__)


def recycle_then_unlink(records, do_unlink):
    """Save a copy of ``records`` in the Recycle Bin, then run ``do_unlink``.

    * The copy and the delete succeed or fail together: if the delete is
      blocked (e.g. "posted entries cannot be deleted"), the copy is undone,
      even when the caller catches the error.
    * Failing to make the copy is logged and never blocks the delete.
    """
    env = records.env
    if (not records or env.context.get('recycle_bin_skip')
            or env.context.get('recycle_bin_saved') == records._name):
        return do_unlink(records)
    Policy = env.get('x.deletion.policy')
    if Policy is not None:
        Policy._enforce(records)   # Critical / Approval rules for a person's delete
    Bin = env.get('x.recycle.bin')
    if Bin is None or not Bin._is_tracked_model(records._name):
        return do_unlink(records)
    with env.cr.savepoint():
        try:
            with env.cr.savepoint():
                Bin._snapshot(records)
        except Exception:  # noqa: BLE001
            _logger.exception("Recycle Bin: could not save a copy of %s %s",
                              records._name, records.ids)
        return do_unlink(records.with_context(recycle_bin_saved=records._name))


class Base(models.AbstractModel):
    _inherit = 'base'

    def unlink(self):
        # Runs after each model's own checks (they call super() last).
        return recycle_then_unlink(self, lambda recs: super(Base, recs).unlink())


# Documents whose standard delete removes their lines BEFORE reaching the
# generic hook above; take the copy first so the lines are included.
class AccountMove(models.Model):
    _inherit = 'account.move'

    def unlink(self):
        return recycle_then_unlink(self, lambda recs: super(AccountMove, recs).unlink())


class StockPicking(models.Model):
    _inherit = 'stock.picking'

    def unlink(self):
        return recycle_then_unlink(self, lambda recs: super(StockPicking, recs).unlink())


class PurchaseRequisition(models.Model):
    _inherit = 'purchase.requisition'

    def unlink(self):
        return recycle_then_unlink(self, lambda recs: super(PurchaseRequisition, recs).unlink())
