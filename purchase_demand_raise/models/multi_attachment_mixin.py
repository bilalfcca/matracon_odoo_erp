from odoo import api, models, _
from odoo.exceptions import UserError

# Upload "holding" model used by the matracon_multi_file widget: files are
# uploaded with this res_model and res_id = 0 so no other module's upload hooks
# run (e.g. Accounting's ir.attachment._post_add_create, which tries to import an
# uploaded file into the vendor bill and crashes on unsaved bills). The files
# are linked to the real record when it is saved (_matracon_bind_attachments).
STAGING_MODEL = 'matracon.multi.attachment.mixin'


class MatraconMultiAttachmentMixin(models.AbstractModel):
    """Support for multi-file upload fields (Many2many → ir.attachment shown
    with widget="matracon_multi_file").

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
        used = [n for n in self._matracon_attachment_field_names() if n in vals]
        before = {}
        if used:
            for rec in self.sudo():
                before[rec.id] = rec._matracon_all_attachments(used)
        res = super().write(vals)
        if used:
            self._matracon_bind_attachments(used)
            self._matracon_delete_removed_attachments(before, used)
        return res

    def _matracon_all_attachments(self, field_names):
        self.ensure_one()
        attachments = self.env['ir.attachment'].sudo()
        for name in field_names:
            attachments |= self.sudo()[name]
        return attachments

    def _matracon_delete_removed_attachments(self, before, field_names):
        """Files cleared or replaced in an upload field are deleted, like
        clearing the old single-file Binary field deleted its file.
        A file is kept when it is
          - still used in another upload field of the same record,
          - owned by another record (e.g. a PR file shared elsewhere), or
          - attached to a chatter message (so message history stays intact).
        """
        Message = self.env['mail.message'].sudo()
        all_names = self._matracon_attachment_field_names()
        for rec in self.sudo():
            removed = before.get(rec.id, self.env['ir.attachment'].sudo()) \
                - rec._matracon_all_attachments(all_names)
            removed = removed.exists().filtered(
                lambda a: a.res_model == rec._name
                and a.res_id == rec.id
                and not a.res_field
            )
            if not removed:
                continue
            in_chatter = Message.search([('attachment_ids', 'in', removed.ids)]).attachment_ids
            for attachment in removed - in_chatter:
                try:
                    with self.env.cr.savepoint():
                        attachment.unlink()
                except UserError:
                    # e.g. Accounting's restricted audit trail forbids deleting a
                    # PDF of a posted bill — it is already removed from the
                    # field, so just keep the file in the database.
                    continue

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
                and a.res_model in (False, rec._name, STAGING_MODEL)
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
_EXT_BY_MIMETYPE = {
    'application/pdf': '.pdf', 'image/jpeg': '.jpg', 'image/jpg': '.jpg', 'image/png': '.png',
}
# ir.attachment columns copied from the legacy file (whatever exists in this DB)
_COPY_COLUMNS = ('type', 'url', 'public', 'db_datas', 'store_fname', 'file_size', 'checksum',
                 'mimetype', 'index_content', 'company_id', 'description',
                 'create_uid', 'create_date', 'write_uid', 'write_date')


def matracon_migrate_binary_to_attachments(env, model_name, binary_field,
                                           filename_field, m2m_field, default_stem):
    """Copy a legacy single-file Binary field into its new multi-file
    Many2many(ir.attachment) field, once per record.

    Done entirely in SQL and memory-safe on any number of files: the legacy
    Binary (attachment=True) is already an ir.attachment row, so the new
    attachment is a copy of that row pointing to the SAME stored file
    (store_fname / checksum). No file content is read, decoded or rewritten.

    - Records whose new field already has files are skipped (idempotent).
    - The legacy Binary value is left in place (hidden) as a safety copy.
    - An identical file already linked to the record (same name and checksum,
      e.g. the copy petty cash used to post in chatter) is reused.
    - The relation rows are inserted directly, so model write() overrides
      (posted-entry locks, chatter notifications, ...) are not triggered.
    Returns the number of records migrated.
    """
    import logging
    _logger = logging.getLogger(__name__)

    if model_name not in env:
        return 0
    Model = env[model_name]
    if binary_field not in Model._fields or m2m_field not in Model._fields:
        return 0
    env.flush_all()
    cr = env.cr
    rel = Model._fields[m2m_field]
    fname_field = Model._fields.get(filename_field) if filename_field else None
    fname_sql = 't."%s"' % filename_field if fname_field and fname_field.store \
        and fname_field.column_type else 'NULL'
    ext_sql = 'CASE a.mimetype %s ELSE \'\' END' % ' '.join(
        "WHEN '%s' THEN '%s'" % kv for kv in _EXT_BY_MIMETYPE.items())

    cr.execute("SELECT column_name FROM information_schema.columns WHERE table_name = 'ir_attachment'")
    existing = {r[0] for r in cr.fetchall()}
    cols = [c for c in _COPY_COLUMNS if c in existing]
    col_list = ', '.join('"%s"' % c for c in cols)
    src_list = ', '.join('a."%s"' % c for c in cols)

    cr.execute("DROP TABLE IF EXISTS _mtr_att_src")
    cr.execute(f"""
        CREATE TEMP TABLE _mtr_att_src AS
        SELECT DISTINCT ON (a.res_id)
               a.id AS src_id, a.res_id AS rec_id, a.checksum,
               COALESCE(NULLIF(TRIM({fname_sql}), ''), %s || {ext_sql}) AS fname,
               NULL::integer AS att_id
          FROM ir_attachment a
          JOIN "{Model._table}" t ON t.id = a.res_id
         WHERE a.res_model = %s AND a.res_field = %s
           AND (a.store_fname IS NOT NULL OR a.db_datas IS NOT NULL)
           AND NOT EXISTS (SELECT 1 FROM "{rel.relation}" r WHERE r."{rel.column1}" = a.res_id)
      ORDER BY a.res_id, a.id DESC
    """, (default_stem, model_name, binary_field))
    # reuse an identical file already on the record
    cr.execute("""
        UPDATE _mtr_att_src s
           SET att_id = (SELECT e.id FROM ir_attachment e
                          WHERE e.res_model = %s AND e.res_id = s.rec_id AND e.res_field IS NULL
                            AND e.name = s.fname AND e.checksum IS NOT DISTINCT FROM s.checksum
                       ORDER BY e.id LIMIT 1)
    """, (model_name,))
    # new attachment rows sharing the legacy file
    cr.execute(f"""
        WITH ins AS (
            INSERT INTO ir_attachment ({col_list}, name, res_model, res_id, res_field)
            SELECT {src_list}, s.fname, %s, s.rec_id, NULL
              FROM _mtr_att_src s JOIN ir_attachment a ON a.id = s.src_id
             WHERE s.att_id IS NULL
         RETURNING id, res_id
        )
        UPDATE _mtr_att_src s SET att_id = ins.id FROM ins
         WHERE ins.res_id = s.rec_id AND s.att_id IS NULL
    """, (model_name,))
    cr.execute(f"""
        INSERT INTO "{rel.relation}" ("{rel.column1}", "{rel.column2}")
        SELECT rec_id, att_id FROM _mtr_att_src WHERE att_id IS NOT NULL
        ON CONFLICT DO NOTHING
    """)
    migrated = cr.rowcount
    cr.execute("DROP TABLE _mtr_att_src")
    env.invalidate_all()
    _logger.info('Multi-file migration: %s.%s → %s: %s record(s)',
                 model_name, binary_field, m2m_field, migrated)
    return migrated
