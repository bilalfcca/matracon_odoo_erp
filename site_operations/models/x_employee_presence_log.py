from odoo import models, fields


def _fmt_duration(seconds):
    """Format a duration in seconds into a human-readable string."""
    s = int(seconds)
    if s < 0:
        return '—'
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f'{h}h {m}m'
    if m:
        return f'{m}m {sec}s'
    return f'{sec}s'


class XEmployeePresenceLog(models.Model):
    """Session-based online history for employees.

    Each record represents one continuous browser session:
      • online_at  — when the browser connected / heartbeat resumed
      • offline_at — when the heartbeat timed out (null = session still active)
      • duration   — computed: offline_at − online_at, or "Ongoing"

    Written by:
      • mail_presence_ext.py  — opens a new session on reconnect
      • hr.employee._cron_update_presence_log — closes the session on heartbeat timeout
    """
    _name = 'x.employee.presence.log'
    _description = 'Employee Online Session History'
    _order = 'online_at desc'
    _rec_name = 'online_at'

    employee_id = fields.Many2one(
        'hr.employee', string='Employee',
        required=True, ondelete='cascade', index=True,
    )
    user_id = fields.Many2one(
        'res.users', string='User',
        required=True, index=True,
    )
    online_at = fields.Datetime(
        string='Online At', required=True, readonly=True,
        help='Timestamp when the browser connected or the heartbeat resumed.',
    )
    offline_at = fields.Datetime(
        string='Offline At', readonly=True,
        help='Timestamp when the heartbeat timed out. '
             'Null while the session is still active.',
    )
    duration = fields.Char(
        string='Duration',
        compute='_compute_duration',
        store=False,
        help='Total time online during this session. '
             '"Ongoing" means the session is still active.',
    )

    def _compute_duration(self):
        for rec in self:
            if not rec.offline_at:
                rec.duration = 'Ongoing'
            else:
                delta = (rec.offline_at - rec.online_at).total_seconds()
                rec.duration = _fmt_duration(delta)
