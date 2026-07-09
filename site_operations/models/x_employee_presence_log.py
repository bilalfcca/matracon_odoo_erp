from odoo import models, fields, api


class XEmployeePresenceLog(models.Model):
    """Audit log of every online/away/offline status change for linked employees.

    Records are written by the `mail.presence` write/create hooks in
    ``mail_presence_ext.py``.  Only status *transitions* are logged —
    repeated heartbeat writes that keep the same status are silently
    skipped to prevent log bloat.
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
        ('away', 'Away'),
        ('offline', 'Offline'),
    ], string='Status', required=True, readonly=True)

    duration = fields.Char(
        string='Duration in State',
        compute='_compute_duration',
        store=False,
        help='How long the employee stayed in this status before the next change. '
             '"Ongoing" means this is still their current status.',
    )

    def _compute_duration(self):
        """For each log entry, duration = time until the NEXT (newer) status change.
        The most recent entry shows "Ongoing" because no newer transition exists yet.

        Uses a single SQL query per employee to avoid N+1 lookups.
        """
        if not self:
            return

        # Group records by employee to batch the lookup
        by_employee = {}
        for log in self:
            by_employee.setdefault(log.employee_id.id, []).append(log)

        for emp_id, logs in by_employee.items():
            timestamps = [l.timestamp for l in logs]
            if not timestamps:
                continue

            # Fetch all log entries for this employee ordered asc so we can
            # look up the "next" entry for each record efficiently
            all_for_emp = self.search([
                ('employee_id', '=', emp_id),
            ], order='timestamp asc')

            # Build a map: timestamp → next_timestamp
            ts_list = [r.timestamp for r in all_for_emp]
            next_ts_map = {}
            for i, r in enumerate(all_for_emp):
                if i + 1 < len(all_for_emp):
                    next_ts_map[r.timestamp] = all_for_emp[i + 1].timestamp
                else:
                    next_ts_map[r.timestamp] = None  # Most recent — still ongoing

            for log in logs:
                next_ts = next_ts_map.get(log.timestamp)
                if next_ts is None:
                    log.duration = 'Ongoing'
                else:
                    delta = next_ts - log.timestamp
                    total_seconds = int(delta.total_seconds())
                    hours, remainder = divmod(total_seconds, 3600)
                    minutes, seconds = divmod(remainder, 60)
                    if hours > 0:
                        log.duration = f'{hours}h {minutes}m'
                    elif minutes > 0:
                        log.duration = f'{minutes}m {seconds}s'
                    else:
                        log.duration = f'{seconds}s'
