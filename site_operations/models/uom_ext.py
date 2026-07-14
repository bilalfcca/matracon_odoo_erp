from odoo import models, fields, api


class UomUomExt(models.Model):
    _inherit = 'uom.uom'

    x_aliases = fields.Char(
        string='Alternative Names',
        help='Comma-separated alternative names / abbreviations that users can '
             'type to find this unit (e.g. "bag, sack, gunny bag"). '
             'The primary Unit Name is always the display label.',
    )

    @api.model
    def _name_search(self, name='', domain=None, operator='ilike', limit=100, order=None):
        """Extend standard name search to also match x_aliases."""
        ids = list(super()._name_search(
            name=name, domain=domain, operator=operator, limit=limit, order=order
        ))
        if name:
            # Search in the alias string — 'ilike' covers partial matches
            alias_domain = (domain or []) + [('x_aliases', 'ilike', name)]
            alias_ids = list(self._search(alias_domain, limit=limit, order=order))
            # Merge preserving order, no duplicates
            existing = set(ids)
            ids += [i for i in alias_ids if i not in existing]
            if limit:
                ids = ids[:limit]
        return ids
