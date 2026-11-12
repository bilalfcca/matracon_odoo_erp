{
    'name': 'Matracon Global Search',
    'version': '19.0.1.0.1',
    'summary': 'Search and filter every record in Odoo from the top bar',
    'description': """
Global search for every app, from the 🔍 icon in the top bar (next to chat and
activities).

* Search as: Everything, Partner, Description / Memo, Reference / Number,
  Amount, Date or Project.
* Filters: record type, status and date range.
* Covers every record type the user can open, picked up automatically,
  including chatter messages and emails.
* Each search runs with the user's own access rights and record rules, so
  people only find records they can already open.
* Read-only: never changes any data.
""",
    'category': 'Productivity',
    'author': 'Matracon',
    'license': 'LGPL-3',
    'depends': ['base', 'web', 'mail'],
    'data': [],
    'assets': {
        'web.assets_backend': [
            'matracon_global_search/static/src/global_search.scss',
            'matracon_global_search/static/src/global_search.xml',
            'matracon_global_search/static/src/global_search.js',
        ],
    },
    'post_init_hook': 'post_init_hook',
    'uninstall_hook': 'uninstall_hook',
    'installable': True,
    'application': False,
}
