"""The two public addresses of the project, kept in one place (the menu links to them).

No template holds an address of its own (`test_no_external_urls`): the page receives these two as
`repo_url` and `docs_url` from `web.deps.render`.
"""

REPO_URL = "https://github.com/Eifel-Joe/reprintbuddy"
DOCS_URL = "https://eifel-joe.github.io/reprintbuddy/"
DOCS_URL_DE = DOCS_URL + "de/"  # the German pages sit under /de/ (mkdocs-static-i18n); the root is English


def docs_url(language: str) -> str:
    """The documentation in the language of the interface (English unless it is German)."""
    return DOCS_URL_DE if language == "de" else DOCS_URL
