from odoo import models, fields, api


class MailPresenceExt(models.Model):
    """Hook into mail.presence to build the employee online/offline history log.

    Only *transitions* (status actually changes) are written to keep the log
    lean — the normal heartbeat that refreshes last_presence without changing
    status is ignored.
    """
    _inherit = 'mail.presence'

    # ── helpers ─────────────────────────────────────────────────────────────

    def _log_presence_change(self, user_id, new_status):
        """Create a presence log entry for the given user if they have an
        employee record linked."""
        employee = self.env['hr.employee'].sudo().search(
            [('user_id', '=', user_id)], limit=1
        )
        if employee:
            self.env['x.employee.presence.log'].sudo().create({
                'employee_id': employee.id,
                'user_id': user_id,
                'timestamp': fields.Datetime.now(),
                'status': new_status,
            })

    # ── ORM hooks ────────────────────────────────────────────────────────────

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        for record in records:
            if record.status:
                self._log_presence_change(record.user_id.id, record.status)
        return records

    def write(self, vals):
        """Log only when the status field actually changes value."""
        if 'status' in vals:
            new_status = vals['status']
            for presence in self:
                if presence.status != new_status:
                    self._log_presence_change(
                        presence.user_id.id, new_status
                    )
        return super().write(vals)
