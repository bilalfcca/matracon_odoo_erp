import re

from odoo import api, fields, models, _
from odoo.exceptions import UserError


class BoardResolution(models.Model):
    _name = 'x.board.resolution'
    _description = 'Board Resolution'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'meeting_date desc, id desc'

    # ── Reference number ──────────────────────────────────────────────────
    #
    # Deliberately NOT a plain ir.sequence counter. A raw ir.sequence's own
    # internal "next number" is independent of whatever text a user actually
    # types into a record's field — if Admin manually overrides one
    # resolution's reference, ir.sequence's counter never finds out, so the
    # *next* auto-suggestion would silently ignore that override. The
    # requirement is explicit that it must not: "next auto-generation should
    # pick up from whatever was last saved, not the original sequence."
    #
    # This mirrors the mechanism Odoo's OWN invoice/PO numbering actually
    # uses under the hood (account.move's SequenceMixin: parse the previous
    # record's own real value, increment it, still fully editable) rather
    # than the lower-level ir.sequence model — same standard-practice
    # category, purpose-built here since our format has no year/month reset
    # requirement (confirmed: numbering is continuous, never resets).
    _REFERENCE_TRAILING_NUMBER_RE = re.compile(r'(\d+)(\D*)$')

    @api.model
    def _default_name(self):
        last = self.search([('name', '!=', False)], order='id desc', limit=1)
        if not last:
            # First-ever resolution: no previous value to continue from —
            # Admin/CEO must type the starting reference by hand.
            return False
        match = self._REFERENCE_TRAILING_NUMBER_RE.search(last.name)
        if not match:
            return False
        num_str, suffix = match.groups()
        next_num_str = str(int(num_str) + 1)
        if num_str.startswith('0'):
            next_num_str = next_num_str.zfill(len(num_str))
        return last.name[:match.start(1)] + next_num_str + suffix

    @api.model
    def _default_meeting_venue(self):
        company = self.env.company
        parts = [p for p in (
            company.street, company.street2,
            ', '.join(filter(None, [company.city, company.state_id.name, company.zip])),
            company.country_id.name,
        ) if p]
        return ', '.join(parts) or False

    @api.model
    def _default_ratification_text(self):
        return _(
            '<p>"RESOLVED FURTHER THAT any one Director of the Company be and is '
            'hereby authorized to do all such acts, deeds and things as may be '
            'necessary or incidental to give effect to the above resolution(s), '
            'and that all actions already taken in this regard be and are hereby '
            'ratified and confirmed."</p>'
        )

    name = fields.Char(
        string='Resolution Reference', required=True, copy=False, tracking=True,
        default=_default_name,
        help='Always editable. Left blank on the very first resolution ever '
             'created, since there is nothing to continue from — type the '
             'starting reference by hand (e.g. "BR # 26/16"). Every '
             'resolution after that defaults to the previous one\'s own '
             'saved reference with its trailing number incremented by one.',
    )
    meeting_date = fields.Date(
        string='Meeting Date', required=True, tracking=True,
        default=fields.Date.context_today,
    )
    meeting_venue = fields.Text(
        string='Meeting Venue', required=True,
        default=_default_meeting_venue,
        help='Defaults to the company\'s registered address — editable per resolution.',
    )
    subject = fields.Char(string='Resolution Subject', required=True, tracking=True)

    attendee_ids = fields.One2many(
        'x.board.resolution.attendee', 'resolution_id', string='Attendees', copy=True,
    )
    clause_ids = fields.One2many(
        'x.board.resolution.clause', 'resolution_id', string='Resolution Clauses', copy=True,
    )
    ratification_text = fields.Html(
        string='Ratification Clause', default=_default_ratification_text,
        help='Standard boilerplate — editable per resolution.',
    )

    # Only relevant when a clause authorizes a specific named individual
    # (e.g. "for SECP representation") — optional, not every resolution
    # needs one. CNIC is pulled live from the partner's own record (the
    # same x_cnic field already used for employees/contacts elsewhere in
    # this system) rather than retyped here.
    authorized_person_id = fields.Many2one(
        'res.partner', string='Authorized Person',
        help='Only needed when a clause authorizes a specific named individual '
             '(e.g. for SECP representation). Leave blank otherwise.',
    )
    authorized_person_cnic = fields.Char(
        string='Authorized Person CNIC', related='authorized_person_id.x_cnic', readonly=True,
    )

    state = fields.Selection([
        ('draft', 'Draft'),
        ('confirmed', 'Confirmed'),
    ], string='Status', default='draft', tracking=True, copy=False)

    company_id = fields.Many2one(
        'res.company', string='Company', required=True, default=lambda self: self.env.company,
    )

    def action_confirm(self):
        for resolution in self:
            if not resolution.attendee_ids:
                raise UserError(_('Please add at least one attendee before confirming.'))
            if not resolution.clause_ids:
                raise UserError(_('Please add at least one resolution clause before confirming.'))
        self.write({'state': 'confirmed'})

    def action_reset_draft(self):
        self.write({'state': 'draft'})

    def action_print(self):
        self.ensure_one()
        return self.env.ref('board_resolutions.action_report_board_resolution').report_action(self)


class BoardResolutionAttendee(models.Model):
    _name = 'x.board.resolution.attendee'
    _description = 'Board Resolution Attendee'
    _order = 'sequence, id'

    resolution_id = fields.Many2one(
        'x.board.resolution', required=True, ondelete='cascade', index=True,
    )
    sequence = fields.Integer(default=10)
    name = fields.Char(string='Name', required=True)
    designation = fields.Char(
        string='Designation', required=True,
        help='E.g. Director, Chief Executive Officer, Company Secretary, ...',
    )
    # Optional — only used to auto-print a signature image. An attendee with
    # no linked user (e.g. an external director with no Odoo login) simply
    # gets a blank line to sign by hand, same "only if configured" rule used
    # everywhere else signatures were automated in this system.
    user_id = fields.Many2one(
        'res.users', string='Linked User (for signature)',
        help='If this user has a signature configured in their own Preferences, '
             'it is printed automatically. Leave blank for a manual signature.',
    )
    signature_image = fields.Binary(related='user_id.x_sign_signature', readonly=True)


class BoardResolutionClause(models.Model):
    _name = 'x.board.resolution.clause'
    _description = 'Board Resolution Clause'
    _order = 'sequence, id'

    resolution_id = fields.Many2one(
        'x.board.resolution', required=True, ondelete='cascade', index=True,
    )
    sequence = fields.Integer(default=10)
    text = fields.Html(string='Clause Text', required=True)
