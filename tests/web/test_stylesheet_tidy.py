"""The stylesheet is tidy (v0.3.8 p73): colours live in variables, and no rule is left without a user.

- Outside `:root` no colour value is written out: it is a variable. There is one dark theme; a light one
  would change the variables only.
- Every variable is used.
- Every class a rule names appears in a template, a script or Python code (as a word, or as the start of
  a name that is put together from a state, such as `tone-` + a tone).
"""

import re
from pathlib import Path

import reprintbuddy

PACKAGE = Path(reprintbuddy.__file__).parent
CSS = (PACKAGE / "web/static/css/reprintbuddy.css").read_text(encoding="utf-8")


def _without_comments(text):
    return re.sub(r"/\*.*?\*/", "", text, flags=re.S)


def _outside_root():
    css = _without_comments(CSS)
    return css[re.search(r":root \{.*?\n\}", css, re.S).end():]


def _sources():
    text = ""
    for path in sorted(PACKAGE.rglob("*")):
        if path.suffix not in {".py", ".html", ".js"} or {"locales", "migrations", "__pycache__"} & set(path.parts):
            continue
        part = path.read_text(encoding="utf-8", errors="ignore")
        if path.suffix == ".html":
            part = re.sub(r"\{#.*?#\}|<!--.*?-->", "", part, flags=re.S)
        elif path.suffix == ".js":
            part = re.sub(r"/\*.*?\*/", "", part, flags=re.S)
            part = re.sub(r"(?m)^\s*//.*$", "", part)
        else:
            part = re.sub(r'(?m)^\s*#.*$|""".*?"""', "", part, flags=re.S)
        text += part + "\n"
    return text


def _selectors():
    """Every selector of every rule (also inside @media / @supports)."""
    found = []

    def walk(text):
        i = 0
        while i < len(text):
            j = text.find("{", i)
            if j < 0:
                return
            head, depth, k = text[i:j].strip(), 1, j + 1
            while depth and k < len(text):
                depth += {"{": 1, "}": -1}.get(text[k], 0)
                k += 1
            if head.startswith(("@media", "@supports")):
                walk(text[j + 1 : k - 1])
            elif not head.startswith("@"):
                found.extend(part.strip() for part in head.split(","))
            i = k

    walk(_without_comments(CSS))
    return found


def test_no_colour_is_written_out_outside_root():
    literals = re.findall(r"#[0-9a-fA-F]{3,8}\b|rgba?\([^)]*\)|hsla?\([^)]*\)", _outside_root())
    assert literals == []


def test_every_variable_is_used():
    css = _without_comments(CSS)
    defined = set(re.findall(r"(--[\w-]+)\s*:", css))
    assert defined, "no variables found"
    unused = [name for name in sorted(defined) if not re.search(rf"var\(\s*{re.escape(name)}\b", css) and name not in _sources()]
    assert unused == []


def test_every_class_in_a_rule_is_used_somewhere():
    sources = _sources()
    dead = []
    for selector in _selectors():
        for name in re.findall(r"\.([A-Za-z_][\w-]*)", selector):
            if re.search(rf"(?<![\w-]){re.escape(name)}(?![\w-])", sources):
                continue
            prefix = name.rsplit("-", 1)[0] + "-" if "-" in name else None  # `tone-` + a tone
            if prefix and re.search(rf"{re.escape(prefix)}(\{{\{{|\{{|['\"] *~|['\"] *\+)", sources):
                continue
            dead.append(selector)
    assert dead == []
