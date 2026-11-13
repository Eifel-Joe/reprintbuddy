"""Every address under /articles, in the order the app matches them (0.4.8, R11).

`web/routes/articles.py` was split into a package. The order of the addresses decides which route
answers (`/articles/new...` and the routers of upload and of articles made of parts before
`/articles/{article_id}...`), so it is frozen here as it was on 0.4.7 (12a69a3): path, methods and the
name of the route function.
"""

from conftest import api_routes

ROUTES = [
    ('/articles/new/upload', 'GET', 'upload_page'),
    ('/articles/new/upload', 'POST', 'upload'),
    ('/articles/{article_id:int}/to-assembled', 'GET', 'to_assembled_form'),
    ('/articles/{article_id:int}/to-assembled', 'POST', 'to_assembled'),
    ('/articles/new-assembled', 'GET', 'assembled_new'),
    ('/articles/new-assembled', 'POST', 'assembled_create'),
    ('/articles/{article_id:int}/assembled', 'GET', 'assembled_edit'),
    ('/articles/{article_id:int}/assembled', 'POST', 'assembled_update'),
    ('/articles', 'GET', 'articles_list'),
    ('/articles/{article_id:int}/target', 'POST', 'article_target'),
    ('/articles/new', 'GET', 'articles_new'),
    ('/articles/new/file/{file_id:int}', 'GET', 'articles_plates'),
    ('/articles/new/file/{file_id:int}/plate/{index:int}/thumbnail', 'GET', 'articles_thumbnail'),
    ('/articles/new/file/{file_id:int}/thumbnail', 'GET', 'articles_file_thumbnail'),
    ('/articles/{article_id:int}/relink/file/{file_id:int}/plate/{index:int}/thumbnail', 'GET', 'relink_thumbnail'),
    ('/articles/new/file/{file_id:int}/plate/{index:int}', 'GET', 'articles_plate_form'),
    ('/articles/new/file/{file_id:int}/plate/{index:int}', 'POST', 'articles_create'),
    ('/articles/{article_id:int}', 'GET', 'article_detail'),
    ('/articles/{article_id:int}/thumbnail', 'GET', 'article_thumbnail'),
    ('/articles/{article_id:int}/edit', 'GET', 'article_edit'),
    ('/articles/{article_id:int}/edit', 'POST', 'article_update'),
    ('/articles/{article_id:int}/filaments', 'GET', 'article_filaments'),
    ('/articles/{article_id:int}/filaments', 'POST', 'article_filaments_save'),
    ('/articles/{article_id:int}/archive', 'POST', 'handler'),
    ('/articles/{article_id:int}/restore', 'POST', 'handler'),
    ('/articles/{article_id:int}/relink', 'GET', 'relink_browse'),
    ('/articles/{article_id:int}/relink/file/{file_id:int}', 'GET', 'relink_plates'),
    ('/articles/{article_id:int}/relink/file/{file_id:int}/plate/{index:int}', 'POST', 'relink_restore'),
    ('/articles/{article_id:int}/resume', 'POST', 'article_resume'),
    ('/articles/{article_id:int}/auto', 'POST', 'article_auto'),
    ('/articles/{article_id:int}/confirm-file', 'POST', 'article_confirm_file'),
    ('/articles/{article_id:int}/qr.{kind}', 'GET', 'picture'),
    ('/articles/{article_id:int}/label.png', 'GET', 'article_preview'),
    ('/articles/{article_id:int}/label', 'POST', 'print_article_label'),
]


def test_the_article_addresses_keep_their_order(client):
    seen = [
        (route.path, ",".join(sorted(route.methods)), route.endpoint.__name__)
        for route in api_routes(client.app)
        if route.path.startswith("/articles")
    ]
    assert seen == ROUTES
