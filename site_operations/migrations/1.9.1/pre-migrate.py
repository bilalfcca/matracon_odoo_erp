import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    """Reset all inherited (extension) form/list/search views owned by
    site_operations to an empty <data/> before module data is reloaded.
    Stored views may still reference fields removed from the code, which
    breaks combined view validation during upgrade. Views still present in
    code are rewritten from XML during this upgrade; views removed from code
    are deleted automatically at the end of the upgrade."""
    cr.execute("""
        SELECT v.id, v.name
          FROM ir_ui_view v
          JOIN ir_model_data d
            ON d.res_id = v.id
           AND d.model = 'ir.ui.view'
           AND d.module = 'site_operations'
         WHERE v.inherit_id IS NOT NULL
           AND v.mode = 'extension'
           AND v.type != 'qweb'
    """)
    rows = cr.fetchall()
    if not rows:
        return
    cr.execute(
        "UPDATE ir_ui_view SET arch_db = jsonb_build_object('en_US', '<data/>') WHERE id = ANY(%s)",
        ([r[0] for r in rows],),
    )
    for view_id, name in rows:
        _logger.info("site_operations pre-migrate: reset view %s (%s)", view_id, name)
