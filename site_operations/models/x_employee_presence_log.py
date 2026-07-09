from odoo import models, fields


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
    timestamp = fields.Datetime(string='When', required=True, readonly=True)
    status = fields.Selection([
        ('online', 'Online'),
        ('away', 'Away'),
        ('offline', 'Offline'),
    ], string='Status', required=True, readonly=True)
