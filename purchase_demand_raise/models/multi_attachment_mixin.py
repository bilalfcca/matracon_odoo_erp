from odoo import api, models, _


class MatraconMultiAttachmentMixin(models.AbstractModel):
    """Support for multi-file upload fields (Many2many → ir.attachment shown
    with widget="many2many_binary").

    The many2many_binary widget uploads each file with res_id = 0 when the
    record is still unsaved.  Odoo only lets the uploader (and admins) read
    an attachment that is not linked to a record, so other users in the
    approval chain would get an Access Error.  This mixin links every newly
    uploaded file to its record (res_model / res_id) on create and write,
    after which normal record access rules apply to the files.
    """
    _name = 'matracon.multi.attachment.mixin'
    _description = 'Matracon Multi-File Attachment Helper'

    def _matracon_attachment_field_names(self):
        # Cached per registry class (rebuilt on every registry reload)
        cls = type(self)
        names = cls.__dict__.get('_matracon_att_field_cache')
        if names is None:
            names = [
                name for name, field in self._fields.items()
                if field.type == 'many2many'
                and field.comodel_name == 'ir.attachment'
                and field.store
            ]
            cls._matracon_att_field_cache = names
        return names

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        names = records._matracon_attachment_field_names()
        used = [n for n in names if any(n in vals for vals in vals_list)]
        if used:
            records._matracon_bind_attachments(used)
        return records

    def write(self, vals):
        res = super().write(vals)
        used = [n for n in self._matracon_attachment_field_names() if n in vals]
        if used:
            self._matracon_bind_attachments(used)
        return res

    def _matracon_bind_attachments(self, field_names=None):
        """Link unbound files in the given attachment fields to their record.
        Files already linked to another record (e.g. a PR attachment shared
        with its alternative RFQs) are left untouched."""
        field_names = field_names or self._matracon_attachment_field_names()
        for rec in self:
            attachments = self.env['ir.attachment'].sudo()
            for name in field_names:
                attachments |= rec.sudo()[name]
            unbound = attachments.filtered(
                lambda a: not a.res_id
                and not a.res_field
                and a.res_model in (False, rec._name)
            )
            if unbound:
                unbound.write({'res_model': rec._name, 'res_id': rec.id})

    def _matracon_action_open_attachments(self, attachments, title=None):
        """Open one file directly, or a list when several files are attached."""
        self.ensure_one()
        if not attachments:
            return False
        if len(attachments) == 1:
            return {
                'type': 'ir.actions.act_url',
                'url': f'/web/content/{attachments.id}?filename={attachments.name}',
                'target': 'new',
            }
        return {
            'type': 'ir.actions.act_window',
            'name': title or _('Attachments'),
            'res_model': 'ir.attachment',
            'view_mode': 'kanban,list,form',
            'domain': [('id', 'in', attachments.ids)],
            'context': {'create': False},
            'target': 'current',
        }


# ── Migration helper ──────────────────────────────────────────────────────────
def _sniff_extension(raw):
    if raw.startswith(b'%PDF'):
        return '.pdf'
    if raw.startswith(b'\xff\xd8\xff'):
        return '.jpg'
    if raw.startswith(b'\x89PNG\r\n\x1a\n'):
        return '.png'
    return ''


def matracon_migrate_binary_to_attachments(env, model_name, binary_field,
                                           filename_field, m2m_field, default_stem):
    """Copy a legacy single-file Binary field into its new multi-file
    Many2many(ir.attachment) field, once per record.

    - Records whose new field already has files are skipped (idempotent).
    - The legacy Binary value is left in place (hidden) as a safety copy.
    - The relation row is inserted directly so model write() overrides
      (posted-entry locks, chatter notifications, …) are not triggered.
    Returns the number of records migrated.
    """
    import base64
    import logging
    _logger = logging.getLogger(__name__)

    if model_name not in env:
        return 0
    Model = env[model_name].sudo().with_context(active_test=False, prefetch_fields=False)
    if binary_field not in Model._fields or m2m_field not in Model._fields:
        return 0
    rel = Model._fields[m2m_field]
    cr = env.cr
    Attachment = env['ir.attachment'].sudo()

    migrated = 0
    for rec in Model.search([(binary_field, '!=', False)]):
        cr.execute(
            f'SELECT 1 FROM "{rel.relation}" WHERE "{rel.column1}" = %s LIMIT 1',
            (rec.id,))
        if cr.fetchone():
            continue
        datas = rec[binary_field]
        if not datas:
            continue
        name = (filename_field and rec[filename_field]) or ''
        if not name:
            try:
                head = base64.b64decode(datas[:64]) if isinstance(datas, (str, bytes)) else b''
            except Exception:
                head = b''
            name = default_stem + (_sniff_extension(head) or '')
        # Reuse an identical file already linked to the record (e.g. the copy
        # petty cash used to post in chatter) instead of storing it twice.
        att = Attachment.search([
            ('res_model', '=', model_name), ('res_id', '=', rec.id),
            ('res_field', '=', False), ('name', '=', name),
        ], limit=1)
        if not att or att.datas != datas:
            att = Attachment.create({
                'name': name,
                'datas': datas,
                'res_model': model_name,
                'res_id': rec.id,
            })
        cr.execute(
            f'INSERT INTO "{rel.relation}" ("{rel.column1}", "{rel.column2}") '
            f'VALUES (%s, %s) ON CONFLICT DO NOTHING',
            (rec.id, att.id))
        migrated += 1
    Model.invalidate_model([m2m_field])
    _logger.info('Multi-file migration: %s.%s → %s: %s record(s)',
                 model_name, binary_field, m2m_field, migrated)
    return migrated
