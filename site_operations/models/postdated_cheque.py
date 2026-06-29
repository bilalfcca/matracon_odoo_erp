from markupsafe import Markup
from odoo import models, fields, api, _
from odoo.exceptions import UserError


class PostdatedCheque(models.Model):
    _name = 'x.postdated.cheque'
    _description = 'Post-Dated Cheque Register'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'cheque_date asc, id asc'

    name = fields.Char(
        string='Cheque No', required=True, tracking=True, copy=False)
    bank_journal_id = fields.Many2one(
        'account.journal', string='Bank', required=True,
        domain=[('type', '=', 'bank')], tracking=True)
    partner_id = fields.Many2one(
        'res.partner', string='Payee', required=True, tracking=True,
        domain="[('category_id.name', 'in', ['Vendor', 'Subcontractor'])]",
    )
    amount = fields.Monetary(
        string='Amount', required=True,
        currency_field='currency_id', tracking=True)
    currency_id = fields.Many2one(
        'res.currency',
        default=lambda self: self.env.company.currency_id)
    issue_date = fields.Date(
        string='Issue Date',
        default=fields.Date.context_today, required=True)
    cheque_date = fields.Date(
        string='Cheque Date (Post-Date)', required=True, tracking=True)
    payment_id = fields.Many2one(
        'account.payment', string='Linked Payment', ondelete='set null')
    state = fields.Selection([
        ('pending', 'Pending Clearance'),
        ('cleared', 'Cleared'),
        ('bounced', 'Bounced'),
        ('cancelled', 'Cancelled'),
    ], default='pending', tracking=True, required=True)
    days_to_clearance = fields.Integer(
        compute='_compute_days_to_clearance', store=True)
    is_overdue = fields.Boolean(
        compute='_compute_days_to_clearance', store=True,
        search='_search_is_overdue')
    remarks = fields.Text(string='Remarks')

    def _search_is_overdue(self, operator, value):
        today = fields.Date.today()
        if (operator == '=' and value) or (operator == '!=' and not value):
            # is_overdue = True: pending AND cheque_date < today
            return [('state', '=', 'pending'), ('cheque_date', '<', today)]
        # is_overdue = False: NOT pending OR cheque_date >= today
        return ['|', ('state', '!=', 'pending'), ('cheque_date', '>=', today)]

    @api.depends('cheque_date', 'state')
    def _compute_days_to_clearance(self):
        today = fields.Date.today()
        for cheque in self:
            if cheque.cheque_date:
                delta = (cheque.cheque_date - today).days
                cheque.days_to_clearance = delta
                cheque.is_overdue = (delta < 0 and cheque.state == 'pending')
            else:
                cheque.days_to_clearance = 0
                cheque.is_overdue = False

    def action_mark_cleared(self):
        for cheque in self:
            if cheque.state != 'pending':
                raise UserError(_(
                    'Only pending cheques can be marked as cleared.'))
            cheque.state = 'cleared'
            cheque.message_post(
                body=Markup(_('Cheque <b>%s</b> marked as cleared.'))
                % cheque.name)

    def action_mark_bounced(self):
        for cheque in self:
            if cheque.state != 'pending':
                raise UserError(_(
                    'Only pending cheques can be marked as bounced.'))
            cheque.state = 'bounced'
            cheque.message_post(
                body=Markup(_('Cheque <b>%s</b> bounced.')) % cheque.name)

    def action_cancel(self):
        for cheque in self:
            cheque.state = 'cancelled'
            cheque.message_post(body=_('Cheque cancelled.'))
