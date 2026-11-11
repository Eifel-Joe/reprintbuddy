"""Reading the row templates of the forms that add rows in place (0.4.1, p380)."""

import re


def without_templates(page: str) -> str:
    """The page as the browser shows it: no <template> (they can nest: a new option holds a row template)."""
    inner = re.compile(r"<template\b[^>]*>(?:(?!<template\b).)*?</template>", re.S)
    while inner.search(page):
        page = inner.sub("", page)
    return page


def templates_of(page: str) -> list[str]:
    """The outermost <template> elements with everything inside them (a nested one stays in its parent)."""
    found, depth, start = [], 0, 0
    for tag in re.finditer(r"<(/?)template\b[^>]*>", page):
        if tag.group(1):
            depth -= 1
            if depth == 0:
                found.append(page[start:tag.end()])
        else:
            if depth == 0:
                start = tag.start()
            depth += 1
    return found
