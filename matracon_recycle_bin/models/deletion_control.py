"""Deletion control: Critical records can't be deleted by normal users;
Approval records need an Admin to approve a deletion request.

Only deletions a *person* starts (Delete in a list/form, or a "Delete" button)
are controlled. Deletions done by the system — the nightly draft cleanup,
Cancel Receipt, Odoo re-syncing lines — run exactly as before.
"""
from markupsafe import Markup

from odoo import api, fields, models, _
from odoo.exceptions import RedirectWarning, UserError
from odoo.tools.safe_eval import safe_eval

ADMIN_GROUPS = ('purchase_demand_raise.group_matracon_admin', 'base.group_system')


def is_deletion_admin(user):
    return any(user.has_group(g) for g in ADMIN_GROUPS)


def user_started_delete(records):
    """True when the current HTTP call is a person deleting these records:
    the Delete action (unlink) or a model's own "Delete" button."""
    try:
        from odoo.http import request
    except ImportError:
        return False
    if not request:
        return False
    params = getattr(request, 'params', None) or {}
    if not isinstance(params, dict) or params.get('model') != records._name:
        return False
    method = str(params.get('method') or '')
    return method == 'unlink' or method.startswith('action_delete')


class DeletionPolicy(models.Model):
    _name = 'x.deletion.policy'
    _description = 'Deletion Rule'
    _order = 'level, model_label'
    _rec_name = 'model_label'

    model = fields.Char(string='Technical Model', required=True, index=True)
    model_label = fields.Char(string='Record Type', compute='_compute_model_label', store=True)
    level = fields.Selection([
        ('critical', 'Critical — only Admin can delete'),
        ('approval', 'Approval — Admin must approve'),
        ('free', 'Free — as before'),
    ], required=True, default='approval')
    apply_domain = fields.Char(
        string='Applies To', default='[]',
        help='Optional filter: the rule only applies to records matching it '
             '(e.g. project tasks with a project, not personal to-dos).')
    active = fields.Boolean(default=True)

    _unique_model = models.Constraint('UNIQUE(model)', 'There is already a rule for this record type.')

    @api.depends('model')
    def _compute_model_label(self):
        for rec in self:
            ir_model = self.env['ir.model'].sudo()._get(rec.model) if rec.model else False
            rec.model_label = ir_model.name if ir_model else rec.model

    @api.model
    def _level_for(self, records):
        """Return (level, controlled_records) for ``records``."""
        rule = self.sudo().search([('model', '=', records._name)], limit=1)
        if not rule or rule.level == 'free':
            return 'free', records.browse()
        domain = safe_eval(rule.apply_domain or '[]')
        controlled = records.sudo().filtered_domain(domain) if domain else records
        return rule.level, records.browse(controlled.ids)

    @api.model
    def _enforce(self, records):
        """Called before a person's delete. Raises for blocked deletions."""
        env = self.env
        if env.context.get('deletion_request_id') or not user_started_delete(records):
            return
        if is_deletion_admin(env.user):
            return
        level, controlled = self._level_for(records)
        if not controlled:
            return
        label = env['ir.model']._get(records._name).name or records._name
        if level == 'critical':
            raise UserError(_(
                '%(type)s records cannot be deleted. They are protected business '
                'records; please archive them instead, or ask an Administrator.'
            ) % {'type': label})
        # approval
        names = ', '.join(controlled.sudo().mapped('display_name')[:5])
        action = env.ref('matracon_recycle_bin.action_deletion_request_wizard')
        raise RedirectWarning(
            _('Deleting %(type)s needs Administrator approval.\n\n%(names)s\n\n'
              'Click "Request Deletion" and give the reason; the record stays until '
              'an Administrator approves.') % {'type': label, 'names': names},
            action.id, _('Request Deletion'),
            {'default_res_model': records._name,
             'default_res_ids': ','.join(str(i) for i in controlled.ids)})


class DeletionRequest(models.Model):
    _name = 'x.deletion.request'
    _description = 'Deletion Request'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'requested_on desc, id desc'

    name = fields.Char(string='Records', required=True, readonly=True)
    res_model = fields.Char(string='Model', required=True, readonly=True, index=True)
    model_label = fields.Char(string='Record Type', readonly=True)
    app_name = fields.Char(string='App / Module', readonly=True)
    res_ids = fields.Char(string='Record IDs', required=True, readonly=True)
    record_count = fields.Integer(string='Count', readonly=True)
    reason = fields.Text(required=True, readonly=True)
    requested_by_id = fields.Many2one('res.users', string='Requested By', readonly=True, index=True)
    requested_on = fields.Datetime(string='Requested On', readonly=True, default=fields.Datetime.now)
    state = fields.Selection([
        ('pending', 'Pending'),
        ('approved', 'Approved & Deleted'),
        ('rejected', 'Rejected'),
        ('cancelled', 'Cancelled'),
    ], default='pending', required=True, readonly=True, tracking=True, index=True)
    decided_by_id = fields.Many2one('res.users', string='Approved / Rejected By', readonly=True)
    decided_on = fields.Datetime(string='Decided On', readonly=True)
    reject_reason = fields.Text(string='Rejection Reason')
    bin_entry_ids = fields.One2many('x.recycle.bin', 'request_id', string='Deleted Records')

    def _records(self):
        self.ensure_one()
        ids = [int(i) for i in (self.res_ids or '').split(',') if i.strip().isdigit()]
        Model = self.env.get(self.res_model)
        return Model.browse(ids).exists() if Model is not None else None

    def action_open_records(self):
        self.ensure_one()
        recs = self._records()
        if not recs:
            raise UserError(_('The records no longer exist.'))
        return {
            'type': 'ir.actions.act_window', 'res_model': self.res_model,
            'name': self.model_label, 'domain': [('id', 'in', recs.ids)],
            'view_mode': 'list,form', 'target': 'current',
        }

    def _check_admin(self):
        if not is_deletion_admin(self.env.user):
            raise UserError(_('Only an Administrator can approve or reject deletion requests.'))

    def action_approve(self):
        self._check_admin()
        for req in self:
            if req.state != 'pending':
                raise UserError(_('Only pending requests can be approved.'))
            recs = req._records()
            if recs:
                recs.with_context(deletion_request_id=req.id).unlink()
            req.write({'state': 'approved', 'decided_by_id': self.env.user.id,
                       'decided_on': fields.Datetime.now()})
            req._close_activities()
            req.message_post(
                body=Markup(_('Deletion <b>approved</b> by %s; records deleted and kept in '
                              'Recycle Bin → Deleted Records.')) % self.env.user.name,
                partner_ids=req.requested_by_id.partner_id.ids)

    def action_reject(self):
        self._check_admin()
        for req in self:
            if req.state != 'pending':
                raise UserError(_('Only pending requests can be rejected.'))
            req.write({'state': 'rejected', 'decided_by_id': self.env.user.id,
                       'decided_on': fields.Datetime.now()})
            req._close_activities()
            req.message_post(
                body=Markup(_('Deletion <b>rejected</b> by %(user)s. %(why)s')) % {
                    'user': self.env.user.name,
                    'why': req.reject_reason or ''},
                partner_ids=req.requested_by_id.partner_id.ids)

    def action_cancel(self):
        for req in self:
            if req.state != 'pending':
                raise UserError(_('Only pending requests can be cancelled.'))
            if req.requested_by_id != self.env.user and not is_deletion_admin(self.env.user):
                raise UserError(_('You can only cancel your own requests.'))
            req.sudo().write({'state': 'cancelled'})
            req.sudo()._close_activities()

    def _close_activities(self):
        self.sudo().activity_ids.unlink()

    def _notify_admins(self):
        group_ids = [g.id for g in (self.env.ref(x, raise_if_not_found=False) for x in ADMIN_GROUPS) if g]
        admins = self.env['res.users'].sudo().search([
            ('group_ids', 'in', group_ids), ('share', '=', False)])
        admins = admins.filtered(lambda u: u.id != 1)   # skip the technical __system__ user
        todo = self.env.ref('mail.mail_activity_data_todo', raise_if_not_found=False)
        for req in self.sudo():
            for admin in admins:
                req.activity_schedule(
                    activity_type_id=todo.id if todo else False, user_id=admin.id,
                    summary=_('Approve deletion: %s') % req.name[:80],
                    note=_('Requested by %(user)s. Reason: %(reason)s') % {
                        'user': req.requested_by_id.name, 'reason': req.reason})


class DeletionRequestWizard(models.TransientModel):
    _name = 'x.deletion.request.wizard'
    _description = 'Request Deletion'

    res_model = fields.Char(required=True)
    res_ids = fields.Char(required=True)
    record_names = fields.Text(string='Records', compute='_compute_record_names')
    reason = fields.Text(string='Reason for deletion', required=True)

    @api.depends('res_model', 'res_ids')
    def _compute_record_names(self):
        for wiz in self:
            Model = self.env.get(wiz.res_model) if wiz.res_model else None
            ids = [int(i) for i in (wiz.res_ids or '').split(',') if i.strip().isdigit()]
            recs = Model.browse(ids).exists() if Model is not None else []
            wiz.record_names = '\n'.join(recs.mapped('display_name')) if recs else ''

    def action_submit(self):
        self.ensure_one()
        Model = self.env.get(self.res_model)
        ids = [int(i) for i in (self.res_ids or '').split(',') if i.strip().isdigit()]
        recs = Model.browse(ids).exists() if Model is not None else None
        if not recs:
            raise UserError(_('The records no longer exist.'))
        recs.check_access('unlink')   # the requester must have delete rights today
        Request = self.env['x.deletion.request'].sudo()
        dup = Request.search([('res_model', '=', self.res_model), ('state', '=', 'pending')]).filtered(
            lambda r: set(recs.ids) & {int(i) for i in r.res_ids.split(',') if i.isdigit()})
        if dup:
            raise UserError(_('A deletion request is already pending for: %s') % dup[0].name)
        Bin = self.env['x.recycle.bin']
        req = Request.create({
            'name': ', '.join(recs.sudo().mapped('display_name'))[:250],
            'res_model': self.res_model,
            'model_label': self.env['ir.model']._get(self.res_model).name,
            'app_name': Bin._app_name_for_model(self.res_model),
            'res_ids': ','.join(str(i) for i in recs.ids),
            'record_count': len(recs),
            'reason': self.reason,
            'requested_by_id': self.env.user.id,
        })
        req._notify_admins()
        return {
            'type': 'ir.actions.client', 'tag': 'display_notification',
            'params': {
                'title': _('Deletion request sent'),
                'message': _('An Administrator will review it. The record stays until approved.'),
                'type': 'success', 'sticky': False,
                'next': {'type': 'ir.actions.act_window_close'},
            },
        }
