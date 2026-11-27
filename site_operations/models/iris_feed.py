"""IRIS / FBR notices feed for Tax Notices & Orders.

IRIS (FBR portal) has a captcha, so nothing logs in to IRIS. Instead the FBR
notification emails of every IRIS account are auto-forwarded to one Odoo
address (mail alias "iris" on ``x.iris.message``). Each email:

  1. is logged as an ``x.iris.message`` (the original email stays in its chatter);
  2. is read (Registration No / NTN, name, tax year, section, title, dates,
     IRIS folder, officer);
  3. is matched to an IRIS Account (Registration No → name → recipient email);
  4. creates a Tax Notice, or updates the open one with the same account,
     section and tax year (follow-ups stay on one notice);
  5. alerts every address in IRIS Alert Recipients.

Folders on the notice mirror IRIS: Inbox → Drafts → Submitted → Completed
(+ Info for FBR facilitation messages). FBR sends no acknowledgement email, so
"Mark Submitted" asks for the IRIS acknowledgement no.
"""
import logging
import re
from datetime import datetime, timedelta

from markupsafe import Markup, escape

from odoo import api, fields, models, tools, _
from odoo.exceptions import UserError, ValidationError

_logger = logging.getLogger(__name__)

REMINDER_PARAM = 'site_operations.iris_reminder_days'
DEFAULT_REMINDER_DAYS = '7,3,1,0'
OPEN_STATES = ('draft', 'open', 'reply_submitted', 'under_proceeding', 'order_filed')
DONE_STATES = ('closed', 'paid', 'annulled', 'vacated')
SUBMITTED_STATES = ('reply_submitted', 'under_proceeding', 'order_filed')

KIND_SELECTION = [
    ('assignment', 'Assignment'),
    ('correspondence', 'Correspondence'),
    ('facilitation', 'Facilitation (Info)'),
    ('other', 'Other FBR Email'),
]

_DATE = r'(\d{1,2}[-/ ][A-Za-z]{3,9}[-/ ]\d{2,4})'


# ── Email reader ──────────────────────────────────────────────────────────────
def _parse_date(text):
    if not text:
        return False
    text = re.sub(r'[/ ]', '-', text.strip())
    for fmt in ('%d-%b-%Y', '%d-%b-%y', '%d-%B-%Y', '%d-%B-%y'):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return False


def _clean(text):
    return re.sub(r'\s+', ' ', text or '').strip(' .,:;-')


def _norm_name(text):
    return re.sub(r'[^A-Z0-9]', '', (text or '').upper())


def _strip_fwd(subject):
    return re.sub(r'^\s*((fwd?|fw|re)\s*:\s*)+', '', subject or '', flags=re.I).strip()


def parse_iris_email(subject, body_html):
    """Read an FBR / IRIS notification email. Returns a dict (empty values
    when not found); ``kind`` is False when it is not an FBR/IRIS email."""
    text = tools.html2plaintext(body_html or '') if body_html else ''
    flat = re.sub(r'[ \t\r\f\v]+', ' ', text)
    one = re.sub(r'\s+', ' ', flat)
    res = dict(kind=False, registration_no=False, taxpayer=False, tax_year=False,
               section=False, title=False, notice_date=False, due_date=False,
               iris_folder=False, action=False, doc_type=False, officer=False)

    m = re.search(r'Registration\s*No\.?\s*:?\s*\**\s*(\d{5,})', one, re.I)
    if m:
        res['registration_no'] = m.group(1)
    m = re.search(r'\bName\s*:\s*\**\s*(.+?)\s*(?:\**\s*Tax\s*Year\b|$)', one, re.I)
    if m:
        res['taxpayer'] = _clean(m.group(1))
    m = re.search(r'Tax\s*Year\s*:?\s*\**\s*(\d{4})', one, re.I)
    if m:
        res['tax_year'] = m.group(1)
    m = re.search(r'subject\s*:?\s*\**\s*(\d{1,3}[A-Z]{0,2}(?:\s*\(\s*\w+\s*\))*)\s*\(([^()]*)\)', one, re.I)
    if m:
        res['section'] = re.sub(r'\s+', '', m.group(1))
        res['title'] = _clean(m.group(2))
    m = re.search(r'\bdated\s+' + _DATE, one, re.I)
    if m:
        res['notice_date'] = _parse_date(m.group(1))
    m = (re.search(r'Due\s*Date\s*:?\s*\**\s*' + _DATE, one, re.I)
         or re.search(r'compliance\s+by\s+\**\s*' + _DATE, one, re.I)
         or re.search(r'\bby\s+\**\s*' + _DATE, one, re.I))
    if m:
        res['due_date'] = _parse_date(m.group(1))
    m = re.search(r'from\s+(\w+)\s+folder', one, re.I)
    if m:
        res['iris_folder'] = m.group(1).title()
    m = re.search(r'new\s+assignment\s+(.+?)\s+against\s+your\s+(\w+)\s+with\s+subject', one, re.I)
    if m:
        res['action'] = _clean(m.group(1))
        res['doc_type'] = m.group(2).title()

    low = one.lower()
    if 'new assignment' in low:
        res['kind'] = 'assignment'
    elif 'new correspondence' in low:
        res['kind'] = 'correspondence'
    elif 'facilitation message' in low or 'not a legal notice' in low:
        res['kind'] = 'facilitation'
    elif res['registration_no'] or ('iris' in low and 'fbr' in low):
        res['kind'] = 'other'

    if res['kind'] == 'facilitation':
        if not res['taxpayer']:
            m = re.search(r'\bDear\s+(?!Sir|Madam|Taxpayer)(.+?)\s*,', one, re.I)
            if m:
                res['taxpayer'] = _clean(m.group(1))
        if not res['title']:
            res['title'] = _strip_fwd(subject) or _('FBR facilitation message')

    # officer: first lines after the compliance sentence, before the IRIS button
    lines = [ln.strip(' *') for ln in flat.splitlines()]
    anchor = None
    for i, ln in enumerate(lines):
        if re.search(r'make\s+compliance|by\s+logging\s+into|your\s+iris\s+account\.?$', ln, re.I):
            anchor = i
    if anchor is not None:
        picked = []
        for ln in lines[anchor + 1:]:
            if not ln:
                if picked:
                    break
                continue
            if re.search(r'login|http|iris|\[|disclaimer|^to:|^from:', ln, re.I):
                break
            picked.append(ln)
            if len(picked) >= 2:
                break
        res['officer'] = _clean(', '.join(picked)) or False
    if not res['title'] and res['kind']:
        res['title'] = _strip_fwd(subject) or False
    return res


# ── IRIS Accounts ─────────────────────────────────────────────────────────────
class IrisAccount(models.Model):
    _name = 'x.iris.account'
    _description = 'IRIS Account'
    _order = 'name'

    name = fields.Char(string='Account Name', required=True,
                       help='Taxpayer name exactly as in IRIS, e.g. MATRACON PAKISTAN (PRIVATE) LIMITED.')
    registration_no = fields.Char(string='Registration No / NTN', required=True, index=True,
                                  help='The "Registration No" shown in FBR emails.')
    email = fields.Char(string='Registered Email',
                        help='Email address registered in IRIS for this account.')
    partner_id = fields.Many2one('res.partner', string='Contact')
    active = fields.Boolean(default=True)
    notes = fields.Text()
    company_id = fields.Many2one('res.company', default=lambda self: self.env.company)
    notice_ids = fields.One2many('x.tax.notice.order', 'x_iris_account_id', string='Notices')
    notice_count = fields.Integer(compute='_compute_counts')
    open_count = fields.Integer(string='Pending', compute='_compute_counts')

    _registration_unique = models.Constraint(
        'UNIQUE(registration_no)', 'This Registration No / NTN already has an IRIS account.')

    @api.depends('notice_ids.state', 'notice_ids.x_iris_folder')
    def _compute_counts(self):
        for acc in self:
            acc.notice_count = len(acc.notice_ids)
            acc.open_count = len(acc.notice_ids.filtered(lambda n: n.x_iris_folder in ('inbox', 'drafts')))

    @api.model
    def _normalize_reg(self, value):
        return re.sub(r'\D', '', value or '')

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('registration_no'):
                vals['registration_no'] = vals['registration_no'].strip()
        return super().create(vals_list)

    def action_view_notices(self):
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id('site_operations.action_iris_notices')
        action['domain'] = [('x_iris_account_id', '=', self.id)]
        action['context'] = {'default_x_iris_account_id': self.id}
        return action

    @api.model
    def _match(self, parsed, emails=()):
        """Find the IRIS account of a parsed email."""
        accounts = self.with_context(active_test=False).search([])
        reg = self._normalize_reg(parsed.get('registration_no'))
        if reg:
            for acc in accounts:
                if self._normalize_reg(acc.registration_no) == reg:
                    return acc
        name = _norm_name(parsed.get('taxpayer'))
        if name:
            for acc in accounts:
                if _norm_name(acc.name) == name:
                    return acc
        emails = {e.lower() for e in emails if e}
        if emails:
            for acc in accounts:
                if acc.email and acc.email.strip().lower() in emails:
                    return acc
        return self.browse()


# ── Alert recipients ──────────────────────────────────────────────────────────
class IrisAlertRecipient(models.Model):
    _name = 'x.iris.alert.recipient'
    _description = 'IRIS Alert Recipient'
    _order = 'name, id'

    name = fields.Char(required=True)
    email = fields.Char(required=True)
    active = fields.Boolean(default=True)

    @api.constrains('email')
    def _check_email(self):
        for rec in self:
            if not tools.single_email_re.match((rec.email or '').strip()):
                raise ValidationError(_('"%s" is not a valid email address.', rec.email))

    @api.model
    def _emails(self):
        return [r.email.strip() for r in self.search([]) if r.email]

    @api.model
    def action_reminder_days(self):
        return {
            'type': 'ir.actions.act_window',
            'name': _('Reminder Days'),
            'res_model': 'x.iris.reminder.wizard',
            'view_mode': 'form',
            'target': 'new',
        }


class IrisReminderWizard(models.TransientModel):
    _name = 'x.iris.reminder.wizard'
    _description = 'IRIS Reminder Days'

    reminder_days = fields.Char(
        required=True,
        default=lambda self: self.env['ir.config_parameter'].sudo().get_param(
            REMINDER_PARAM, DEFAULT_REMINDER_DAYS),
        help='Days before the due date on which a reminder is sent, separated by commas. '
             '0 = on the due date. Overdue notices are reminded every day.')

    def action_save(self):
        days = parse_reminder_days(self.reminder_days)
        if not days:
            raise UserError(_('Enter days separated by commas, e.g. 7,3,1,0'))
        self.env['ir.config_parameter'].sudo().set_param(
            REMINDER_PARAM, ','.join(str(d) for d in sorted(days, reverse=True)))
        return {'type': 'ir.actions.act_window_close'}


def parse_reminder_days(text):
    days = set()
    for part in re.split(r'[,\s;]+', text or ''):
        if part.strip().isdigit():
            days.add(int(part))
    return days


# ── Incoming FBR emails (mail alias target) ───────────────────────────────────
class IrisMessage(models.Model):
    _name = 'x.iris.message'
    _description = 'Incoming FBR / IRIS Email'
    _inherit = ['mail.thread']
    _order = 'received_date desc, id desc'

    name = fields.Char(string='Email Subject')
    email_from = fields.Char(string='From')
    email_to = fields.Char(string='To')
    received_date = fields.Datetime(default=fields.Datetime.now)
    body_html = fields.Html(sanitize=True)
    status = fields.Selection([
        ('processed', 'Linked to Notice'),
        ('unknown_account', 'Unknown IRIS Account'),
        ('ignored', 'Not an FBR Email'),
    ], default='ignored', index=True)
    kind = fields.Selection(KIND_SELECTION)
    account_id = fields.Many2one('x.iris.account', string='IRIS Account', index=True)
    notice_id = fields.Many2one('x.tax.notice.order', string='Tax Notice', index=True, ondelete='set null')
    registration_no = fields.Char()
    taxpayer = fields.Char(string='Name in Email')
    tax_year = fields.Char()
    section = fields.Char()
    title = fields.Char()
    action = fields.Char(string='IRIS Action')
    iris_folder = fields.Char(string='IRIS Folder')
    officer = fields.Char()
    notice_date = fields.Date()
    due_date = fields.Date()

    @api.model
    def message_new(self, msg_dict, custom_values=None):
        received = msg_dict.get('date')
        try:
            received = fields.Datetime.to_datetime(received) if received else fields.Datetime.now()
        except (ValueError, TypeError):
            received = fields.Datetime.now()
        vals = dict(custom_values or {})
        vals.update({
            'name': msg_dict.get('subject') or _('(no subject)'),
            'email_from': msg_dict.get('email_from'),
            'email_to': ', '.join(filter(None, [msg_dict.get('to'), msg_dict.get('cc')])),
            'received_date': received,
            'body_html': msg_dict.get('body'),
        })
        rec = self.create(vals)
        rec._process()
        return rec

    def _recipient_emails(self):
        self.ensure_one()
        found = set(tools.email_split((self.email_to or '') + ',' + (self.email_from or '')))
        text = tools.html2plaintext(self.body_html or '')
        for line in text.splitlines():
            if re.match(r'\s*\**\s*(to|cc)\s*:', line, re.I):
                found.update(tools.email_split(line))
        return {e.lower() for e in found}

    def action_reprocess(self):
        for rec in self:
            rec._process()
        return True

    def _process(self):
        Notice = self.env['x.tax.notice.order'].sudo()
        for rec in self:
            parsed = parse_iris_email(rec.name, rec.body_html)
            vals = {k: parsed[k] for k in ('kind', 'registration_no', 'tax_year', 'section', 'title',
                                           'action', 'iris_folder', 'officer', 'notice_date', 'due_date')}
            vals['taxpayer'] = parsed['taxpayer']
            if not parsed['kind']:
                vals['status'] = 'ignored'
                rec.write(vals)
                continue
            account = self.env['x.iris.account'].sudo()._match(parsed, rec._recipient_emails())
            vals['account_id'] = account.id
            vals['status'] = 'processed' if account else 'unknown_account'
            rec.write(vals)
            if rec.notice_id:
                # reprocessing an email already on a notice: only fill the account in
                if account and not rec.notice_id.x_iris_account_id:
                    rec.notice_id.write({'x_iris_account_id': account.id,
                                         'taxpayer_name': account.name,
                                         'partner_id': account.partner_id.id or rec.notice_id.partner_id.id})
                continue
            notice, is_new = Notice._iris_upsert(rec, account)
            rec.notice_id = notice.id
            notice._iris_send_alert(rec, is_new)


# ── Tax notice: IRIS fields, folders, reply, submission ──────────────────────
class TaxNoticeIris(models.Model):
    _name = 'x.tax.notice.order'
    _inherit = ['x.tax.notice.order', 'matracon.multi.attachment.mixin']

    x_iris_account_id = fields.Many2one('x.iris.account', string='IRIS Account', index=True, tracking=True)
    x_iris_kind = fields.Selection(KIND_SELECTION, string='FBR Email Type', tracking=True)
    x_iris_title = fields.Char(string='IRIS Subject', tracking=True)
    x_iris_action = fields.Char(string='IRIS Action', tracking=True,
                                help='e.g. Jurisdiction Assumed, Clarification required')
    x_iris_source_folder = fields.Char(string='Open in IRIS Folder',
                                       help='IRIS folder the latest email says to open the assignment from.')
    x_iris_officer = fields.Char(string='Officer')
    x_iris_from_email = fields.Boolean(string='Created from FBR Email', readonly=True)
    x_iris_message_ids = fields.One2many('x.iris.message', 'notice_id', string='FBR Emails')
    x_iris_message_count = fields.Integer(compute='_compute_x_iris_message_count')
    x_iris_folder = fields.Selection([
        ('inbox', 'Inbox'),
        ('drafts', 'Drafts'),
        ('submitted', 'Submitted'),
        ('completed', 'Completed'),
        ('info', 'Info'),
    ], string='Folder', compute='_compute_x_iris_folder', store=True, index=True)
    x_reply_started = fields.Boolean(string='Reply in Progress', copy=False)
    x_reply_draft = fields.Html(string='Reply Draft', copy=False)
    x_reply_attachment_ids = fields.Many2many(
        'ir.attachment', 'x_tax_notice_reply_attachment_rel', 'notice_id', 'attachment_id',
        string='Reply Documents', copy=False)
    x_iris_ack_no = fields.Char(string='IRIS Acknowledgement No', copy=False, tracking=True)
    x_iris_submitted_date = fields.Date(string='Submitted in IRIS on', copy=False, tracking=True)
    x_iris_slip_ids = fields.Many2many(
        'ir.attachment', 'x_tax_notice_iris_slip_rel', 'notice_id', 'attachment_id',
        string='Acknowledgement Slip', copy=False)

    @api.depends('state', 'x_iris_kind', 'x_reply_started')
    def _compute_x_iris_folder(self):
        for rec in self:
            if rec.state in DONE_STATES:
                rec.x_iris_folder = 'completed'
            elif rec.state in SUBMITTED_STATES:
                rec.x_iris_folder = 'submitted'
            elif rec.x_iris_kind == 'facilitation':
                rec.x_iris_folder = 'info'
            elif rec.x_reply_started:
                rec.x_iris_folder = 'drafts'
            else:
                rec.x_iris_folder = 'inbox'

    def _compute_x_iris_message_count(self):
        for rec in self:
            rec.x_iris_message_count = len(rec.x_iris_message_ids)

    # ---- buttons ----
    def action_start_reply(self):
        self.filtered(lambda r: r.state in ('draft', 'open')).write({'x_reply_started': True})
        return True

    def action_iris_mark_submitted(self):
        self.ensure_one()
        if self.state not in ('draft', 'open'):
            raise UserError(_('Only notices in Inbox or Drafts can be marked as submitted.'))
        return {
            'type': 'ir.actions.act_window',
            'name': _('Mark Submitted in IRIS'),
            'res_model': 'x.iris.submit.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {'default_notice_id': self.id},
        }

    def action_view_iris_messages(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('FBR Emails'),
            'res_model': 'x.iris.message',
            'view_mode': 'list,form',
            'domain': [('notice_id', '=', self.id)],
            'context': {'create': False},
        }

    # ---- email → notice ----
    @api.model
    def _iris_find_open(self, account, msg):
        domain = [('state', 'in', OPEN_STATES), ('x_iris_from_email', '=', True)]
        if account:
            domain.append(('x_iris_account_id', '=', account.id))
        else:
            domain += [('x_iris_account_id', '=', False), ('taxpayer_name', '=ilike', msg.taxpayer or '')]
        if msg.kind == 'facilitation':
            domain += [('x_iris_kind', '=', 'facilitation'), ('tax_year', '=', msg.tax_year or False),
                       ('x_iris_title', '=ilike', msg.title or '')]
        else:
            if not msg.section:
                return self.browse()
            domain += [('notice_section', '=', msg.section), ('tax_year', '=', msg.tax_year or False),
                       ('x_iris_kind', '!=', 'facilitation')]
        return self.search(domain, order='id desc', limit=1)

    @api.model
    def _iris_upsert(self, msg, account):
        """Create or update the tax notice of an incoming FBR email → (notice, is_new)."""
        received = (msg.received_date or fields.Datetime.now()).date()
        doc_date = msg.notice_date or received
        description = ' – '.join(filter(None, [msg.section, msg.title])) or msg.name
        notice = self._iris_find_open(account, msg)
        log = Markup('<b>%s</b><br/>%s') % (
            _('FBR email received: %s', msg.name or ''),
            Markup('<br/>').join(escape(x) for x in filter(None, [
                msg.action and _('Action: %s', msg.action),
                msg.due_date and _('Comply by: %s', msg.due_date),
                msg.iris_folder and _('Open from IRIS folder: %s', msg.iris_folder),
                msg.officer and _('Officer: %s', msg.officer),
            ])))
        if notice:
            vals = {'x_iris_title': msg.title or notice.x_iris_title,
                    'x_iris_action': msg.action or notice.x_iris_action,
                    'x_iris_source_folder': msg.iris_folder or notice.x_iris_source_folder,
                    'x_iris_officer': msg.officer or notice.x_iris_officer,
                    'x_iris_kind': msg.kind}
            if msg.due_date:
                vals['due_date'] = msg.due_date
                if notice.document_date and msg.due_date < notice.document_date:
                    vals['document_date'] = msg.due_date
            if msg.kind != 'facilitation' and notice.state in SUBMITTED_STATES + ('draft',):
                # FBR came back after the reply (clarification, order ...) → Inbox again
                vals.update({'state': 'open', 'case_status': 'open', 'x_reply_started': False})
            notice.write(vals)
            notice.message_post(body=log, subtype_xmlid='mail.mt_note')
            return notice, False
        if msg.due_date and msg.due_date < doc_date:
            doc_date = msg.due_date
        notice = self.create({
            'taxpayer_name': account.name or msg.taxpayer or _('Unknown IRIS account'),
            'partner_id': account.partner_id.id or False,
            'x_iris_account_id': account.id or False,
            'notice_section': msg.section or False,
            'tax_year': msg.tax_year or False,
            'document_date': doc_date,
            'due_date': msg.due_date or False,
            'description': description,
            'x_iris_kind': msg.kind,
            'x_iris_title': msg.title,
            'x_iris_action': msg.action,
            'x_iris_source_folder': msg.iris_folder,
            'x_iris_officer': msg.officer,
            'x_iris_from_email': True,
            'state': 'open',
            'case_status': 'open',
        })
        notice.message_post(body=log, subtype_xmlid='mail.mt_note')
        return notice, True

    # ---- alerts ----
    @api.model
    def _iris_mail_from(self):
        company = self.env.company
        return company.email_formatted or self.env['ir.mail_server']._get_default_from_address()

    def _iris_url(self):
        base = self.env['ir.config_parameter'].sudo().get_param('web.base.url', '')
        return '%s/odoo/x.tax.notice.order/%s' % (base.rstrip('/'), self.id)

    @api.model
    def _iris_send_mail(self, subject, body_html):
        emails = self.env['x.iris.alert.recipient'].sudo()._emails()
        if not emails:
            _logger.info('IRIS alert "%s" not sent: no IRIS Alert Recipients configured.', subject)
            return False
        self.env['mail.mail'].sudo().create({
            'subject': subject,
            'body_html': body_html,
            'email_from': self._iris_mail_from(),
            'email_to': ', '.join(emails),
            'auto_delete': True,
        })
        return True

    def _iris_row_html(self):
        self.ensure_one()
        return Markup(
            '<tr><td style="padding:4px 8px;border:1px solid #ddd">%s</td>'
            '<td style="padding:4px 8px;border:1px solid #ddd">%s</td>'
            '<td style="padding:4px 8px;border:1px solid #ddd">%s</td>'
            '<td style="padding:4px 8px;border:1px solid #ddd">%s</td>'
            '<td style="padding:4px 8px;border:1px solid #ddd"><b>%s</b></td>'
            '<td style="padding:4px 8px;border:1px solid #ddd"><a href="%s">%s</a></td></tr>'
        ) % (self.x_iris_account_id.name or self.taxpayer_name or '', self.notice_section or '',
             self.x_iris_title or self.description or '', self.tax_year or '',
             self.due_date or '', self._iris_url(), self.name)

    _TABLE_HEAD = Markup(
        '<table style="border-collapse:collapse;font-size:13px"><tr style="background:#f2f2f2">'
        '<th style="padding:4px 8px;border:1px solid #ddd">IRIS Account</th>'
        '<th style="padding:4px 8px;border:1px solid #ddd">Section</th>'
        '<th style="padding:4px 8px;border:1px solid #ddd">Subject</th>'
        '<th style="padding:4px 8px;border:1px solid #ddd">Tax Year</th>'
        '<th style="padding:4px 8px;border:1px solid #ddd">Due Date</th>'
        '<th style="padding:4px 8px;border:1px solid #ddd">Odoo</th></tr>')

    def _iris_send_alert(self, msg, is_new):
        self.ensure_one()
        label = _('New FBR notice') if is_new else _('FBR update')
        if self.x_iris_kind == 'facilitation':
            label = _('FBR information')
        account = self.x_iris_account_id.name or _('UNKNOWN IRIS ACCOUNT (Reg. No %s)', msg.registration_no or '-')
        subject = 'IRIS: %s – %s – %s%s' % (
            label, account, ' '.join(filter(None, [self.notice_section, self.tax_year and '(TY %s)' % self.tax_year])) or msg.name,
            self.due_date and _(' – due %s', self.due_date.strftime('%d-%b-%Y')) or '')
        body = Markup('<p>%s</p>') % _('%(label)s received for %(account)s.', label=label, account=account)
        if msg.action:
            body += Markup('<p>%s</p>') % _('IRIS action: %s', msg.action)
        if not self.x_iris_account_id:
            body += Markup('<p style="color:#b00">%s</p>') % _(
                'This Registration No is not in IRIS Accounts. Add the account in Odoo, '
                'then use "Reprocess" on the email.')
        body += self._TABLE_HEAD + self._iris_row_html() + Markup('</table>')
        self._iris_send_mail(subject, body)

    @api.model
    def _cron_iris_reminders(self):
        """Daily digest to IRIS Alert Recipients: overdue + due on the reminder days."""
        days = parse_reminder_days(self.env['ir.config_parameter'].sudo().get_param(
            REMINDER_PARAM, DEFAULT_REMINDER_DAYS))
        today = fields.Date.context_today(self)
        pending = self.sudo().search([
            ('state', 'in', ('draft', 'open')), ('due_date', '!=', False),
        ], order='due_date, id')
        overdue = pending.filtered(lambda n: n.due_date < today)
        buckets = []
        if overdue:
            buckets.append((_('Overdue – not yet submitted'), overdue))
        for d in sorted(days):
            recs = pending.filtered(lambda n, d=d: n.due_date == today + timedelta(days=d))
            if recs:
                buckets.append((_('Due today') if d == 0 else _('Due in %s day(s)', d), recs))
        if not buckets:
            return True
        body = Markup('<p>%s</p>') % _('IRIS / FBR notices that need attention (%s):', today.strftime('%d-%b-%Y'))
        for title, recs in buckets:
            body += Markup('<h3 style="margin:14px 0 6px">%s (%s)</h3>') % (title, len(recs))
            body += self._TABLE_HEAD
            for rec in recs:
                body += rec._iris_row_html()
            body += Markup('</table>')
        subject = _('IRIS reminders: %(over)s overdue, %(total)s due – %(date)s',
                    over=len(overdue), total=sum(len(r) for _t, r in buckets) - len(overdue),
                    date=today.strftime('%d-%b-%Y'))
        self._iris_send_mail(subject, body)
        return True


class IrisSubmitWizard(models.TransientModel):
    _name = 'x.iris.submit.wizard'
    _description = 'Mark Tax Notice Submitted in IRIS'

    notice_id = fields.Many2one('x.tax.notice.order', required=True)
    ack_no = fields.Char(string='IRIS Acknowledgement No', required=True)
    submitted_date = fields.Date(string='Submitted on', required=True, default=fields.Date.context_today)
    slip = fields.Binary(string='Acknowledgement Slip (optional)')
    slip_name = fields.Char()
    remarks = fields.Text()

    def action_confirm(self):
        self.ensure_one()
        notice = self.notice_id
        if notice.state not in ('draft', 'open'):
            raise UserError(_('This notice is already submitted or closed.'))
        vals = {
            'x_iris_ack_no': self.ack_no.strip(),
            'x_iris_submitted_date': self.submitted_date,
            'state': 'reply_submitted',
            'case_status': 'in_process',
            'x_reply_started': False,
        }
        if self.slip:
            att = self.env['ir.attachment'].create({
                'name': self.slip_name or 'IRIS acknowledgement %s' % self.ack_no,
                'datas': self.slip,
                'res_model': notice._name,
                'res_id': notice.id,
            })
            vals['x_iris_slip_ids'] = [(4, att.id)]
        notice.write(vals)
        notice.message_post(body=Markup('%s<br/>%s%s') % (
            _('Reply submitted in IRIS on %s.', self.submitted_date),
            _('Acknowledgement No: %s', self.ack_no),
            Markup('<br/>%s') % self.remarks if self.remarks else ''), subtype_xmlid='mail.mt_note')
        return {'type': 'ir.actions.act_window_close'}
