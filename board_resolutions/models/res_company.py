from odoo import fields, models


class ResCompany(models.Model):
    _inherit = 'res.company'

    # Genuinely used (unlike the x_ceo_name/x_po_officer_name-style fields
    # removed from this same model in a prior session as dead code) — read by
    # the Board Resolution report to print the company's round stamp/seal
    # next to every attendee's signature. One company-wide image, not a
    # per-resolution or per-attendee upload — a company only has one seal.
    x_board_resolution_stamp = fields.Binary(
        string='Company Stamp / Seal',
        attachment=True,
        help='Uploaded once. Printed next to every attendee signature on the '
             'Board Resolution PDF. Leave blank to omit the stamp image.',
    )
