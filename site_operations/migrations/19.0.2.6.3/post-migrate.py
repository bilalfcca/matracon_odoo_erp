"""Paid leave (L) now counts as a paid day.

Recompute day counts only for attendance lines on Draft / Verified sheets.
Posted sheets (and the salary sheets generated from them) keep their figures.
"""
from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    lines = env['x.attendance.line'].search([('sheet_id.state', 'in', ('draft', 'verified'))])
    if not lines:
        return
    Line = env['x.attendance.line']
    for fname in ('present_days', 'absent_days', 'leave_days', 'holiday_days',
                  'paid_days', 'total_days', 'total_display'):
        env.add_to_compute(Line._fields[fname], lines)
    env.flush_all()
