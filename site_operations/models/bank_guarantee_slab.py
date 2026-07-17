"""
Tiered cash-margin slab models for Bank Guarantees.

Two models are defined here:
  - x.bg.facility.cash.margin.slab — template slabs stored on the Bank Facility.
    When a BG is created under a facility these slabs are auto-copied to the BG.
  - x.bg.cash.margin.slab — per-BG slab lines used to compute the margin amount.

Tier computation (for a BG of amount B, slab from F to T at rate P):
  - applicable = min(B, T) - F   (when T > 0, i.e. not the last unlimited tier)
  - applicable = B - F            (when T == 0, meaning "unlimited / remainder")
  - slab_amount = max(0, applicable) × P / 100

Example: BG = 1.8 B
  Tier 1: 0 – 1 B @ 5%  →  applicable = 1 B  →  50 M
  Tier 2: 1 B – 0 (unl) @ 10% →  applicable = 0.8 B →  80 M
  Total margin = 130 M  (effective rate ≈ 7.22%)
"""
from odoo import models, fields, api


class BgFacilityCashMarginSlab(models.Model):
    """Template cash-margin slabs on a Bank Guarantee Facility.
    Copied automatically to any BG issued under this facility."""
    _name = 'x.bg.facility.cash.margin.slab'
    _description = 'Bank Guarantee Facility — Cash Margin Slab Template'
    _order = 'from_amount asc, id asc'

    facility_id = fields.Many2one(
        'x.bank.guarantee.facility', required=True,
        ondelete='cascade', index=True)
    from_amount = fields.Monetary(
        string='From Amount', currency_field='currency_id', default=0,
        help='Lower bound of this tier (inclusive). Use 0 for the first tier.')
    to_amount = fields.Monetary(
        string='Up To Amount', currency_field='currency_id', default=0,
        help='Upper bound of this tier. Set 0 for the last (unlimited) tier.')
    margin_percent = fields.Float(
        string='Cash Margin %', digits=(5, 2))
    currency_id = fields.Many2one(
        related='facility_id.currency_id', store=True, readonly=True)


class BgCashMarginSlab(models.Model):
    """Tiered cash-margin slab lines on a Bank Guarantee.

    When at least one slab is present on the BG, the total Cash Margin Amount
    is computed as the sum of each slab's margin amount.  The flat
    'Cash Margin (%)' field on the BG is ignored in that case.
    """
    _name = 'x.bg.cash.margin.slab'
    _description = 'Bank Guarantee — Cash Margin Slab'
    _order = 'from_amount asc, id asc'

    bg_id = fields.Many2one(
        'x.bank.guarantee', required=True,
        ondelete='cascade', index=True)
    from_amount = fields.Monetary(
        string='From Amount', currency_field='currency_id', default=0,
        help='Lower bound of this tier (inclusive). Use 0 for the first tier.')
    to_amount = fields.Monetary(
        string='Up To Amount', currency_field='currency_id', default=0,
        help='Upper bound of this tier. Set 0 for the last (unlimited) tier.')
    margin_percent = fields.Float(
        string='Cash Margin %', digits=(5, 2))
    applicable_amount = fields.Monetary(
        string='Applicable Amount',
        compute='_compute_slab', store=True,
        currency_field='currency_id',
        help='Portion of the BG amount falling within this tier.')
    slab_amount = fields.Monetary(
        string='Margin Amount',
        compute='_compute_slab', store=True,
        currency_field='currency_id',
        help='Cash margin for this tier = Applicable Amount × Cash Margin %.')
    currency_id = fields.Many2one(
        related='bg_id.currency_id', store=True, readonly=True)

    @api.depends('bg_id.bg_amount', 'from_amount', 'to_amount', 'margin_percent')
    def _compute_slab(self):
        for slab in self:
            bg_amount = slab.bg_id.bg_amount or 0.0
            from_amt = slab.from_amount or 0.0
            to_amt = slab.to_amount or 0.0
            if bg_amount <= from_amt:
                applicable = 0.0
            elif to_amt == 0:
                # Open-ended last tier: everything above from_amount
                applicable = bg_amount - from_amt
            else:
                applicable = min(bg_amount, to_amt) - from_amt
            slab.applicable_amount = max(0.0, applicable)
            slab.slab_amount = slab.applicable_amount * (slab.margin_percent or 0.0) / 100.0
