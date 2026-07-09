from odoo import models, fields, api


class XEmployeePresenceLog(models.Model):
    """Audit log of every online/away/offline status change for linked employees.

    Records are written by:
      • mail_presence_ext.py  — online/away transitions (when browser connects)
      • hr.employee._cron_update_presence_log — offline transitions (when browser closes)

    Only status *transitions* are logged — heartbeat writes that keep the same
    status are silently skipped to keep the log lean.
    """
    _name = 'x.employee.presence.log'
    _description = 'Employee Online/Offline Presence History'
    _order = 'timestamp desc'
    _rec_name = 'timestamp'

    employee_id = fields.Many2one(
        'hr.employee', string='Employee',
        required=True, ondelete='cascade', index=True,
    )
    user_id = fields.Many2one(
        'res.users', string='User',
        required=True, index=True,
    )
    timestamp = fields.Datetime(string='Date & Time', required=True, readonly=True)
    status = fields.Selection([
        ('online', 'Online'),
        ('offline', 'Offline'),
    ], string='Status', required=True, readonly=True)

    duration = fields.Char(
        string='Duration',
        compute='_compute_duration',
        store=False,
        help='How long the employee stayed in this status before the next transition. '
             '"Ongoing" means this is still their current status.',
    )

    def _compute_duration(self):
        """Compute duration = time until the NEXT log entry for the same employee.

        Uses a batch SQL query per unique employee to avoid N+1 lookups.
        Most-recent entry shows 'Ongoing'.
        """
        if not self:
            return

        # Group by employee
        by_employee: dict[int, list] = {}
        for log in self:
            by_employee.setdefault(log.employee_id.id, []).append(log)

        for emp_id, logs in by_employee.items():
            # Fetch all entries for this employee in ascending order
            self.env.cr.execute("""
                SELECT id, timestamp
                FROM   x_employee_presence_log
                WHERE  employee_id = %s
                ORDER  BY timestamp ASC
            """, (emp_id,))
            rows = self.env.cr.fetchall()   # [(id, timestamp), ...]

            # Build map: id → next_timestamp
            next_ts_map = {}
            for i, (rid, rts) in enumerate(rows):
                next_ts_map[rid] = rows[i + 1][1] if i + 1 < len(rows) else None

            for log in logs:
                next_ts = next_ts_map.get(log.id)
                if next_ts is None:
                    log.duration = 'Ongoing'
                else:
                    delta = next_ts - log.timestamp
                    total_seconds = int(delta.total_seconds())
                    if total_seconds < 0:
                        log.duration = '—'
                        continue
                    hours, rem = divmod(total_seconds, 3600)
                    minutes, seconds = divmod(rem, 60)
                    if hours > 0:
                        log.duration = f'{hours}h {minutes}m'
                    elif minutes > 0:
                        log.duration = f'{minutes}m {seconds}s'
                    else:
                        log.duration = f'{seconds}s'
