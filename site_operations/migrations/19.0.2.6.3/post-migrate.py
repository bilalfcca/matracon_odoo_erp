"""Paid leave (L) now counts as a paid day.

Recompute day counts only for attendance lines on Draft / Verified sheets.
Posted sheets (and the salary sheets generated from them) keep their figures.

Memory-safe: line ids come from SQL and are recomputed in small batches,
with the cache cleared after each batch.
"""
import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)
BATCH = 500
FIELDS = ('present_days', 'absent_days', 'leave_days', 'holiday_days',
          'paid_days', 'total_days', 'total_display')


def migrate(cr, version):
    cr.execute("""
        SELECT l.id FROM x_attendance_line l
          JOIN x_attendance_sheet s ON s.id = l.sheet_id
         WHERE s.state IN ('draft', 'verified')
      ORDER BY l.id
    """)
    ids = [r[0] for r in cr.fetchall()]
    env = api.Environment(cr, SUPERUSER_ID, {})
    Line = env['x.attendance.line']
    fields_ = [Line._fields[f] for f in FIELDS if f in Line._fields]
    for i in range(0, len(ids), BATCH):
        lines = Line.browse(ids[i:i + BATCH])
        for field in fields_:
            env.add_to_compute(field, lines)
        env.flush_all()
        env.invalidate_all()
    _logger.info("Attendance: recomputed day counts on %s draft/verified line(s)", len(ids))
