"""The stylesheet is tidy (v0.3.8 p73): colours live in variables, and no rule is left without a user.

- Outside `:root` no colour value is written out: it is a variable. There is one dark theme; a light one
  would change the variables only.
- Every variable is used.
- Every class a rule names is used (v0.3.9 p121): it stands as a word where a class is named (a class
  attribute, `classList.add(...)`, `className = ...`), not in any other text. A name that is put together
  (`tone-` + a tone, or a class that comes from a variable) counts only when the missing part is a
  value of its own in the source (a quoted "bad"), so the prefix alone does not keep a rule alive.
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


def _without_source_comments(suffix, part):
    if suffix == ".html":
        return re.sub(r"\{#.*?#\}|<!--.*?-->", "", part, flags=re.S)
    if suffix == ".js":
        part = re.sub(r"/\*.*?\*/", "", part, flags=re.S)
        return re.sub(r"(?m)^\s*//.*$", "", part)
    # Two steps: with one pattern and re.S the `.*` of a comment line ran to the end of the file
    # and cut away everything after the first comment of every Python file.
    part = re.sub(r"(?m)^[ \t]*#.*$", "", part)
    return re.sub(r'""".*?"""', "", part, flags=re.S)


def _sources():
    parts = []
    for path in sorted(PACKAGE.rglob("*")):
        if path.suffix not in {".py", ".html", ".js"} or {"locales", "migrations", "__pycache__"} & set(path.parts):
            continue
        parts.append(_without_source_comments(path.suffix, path.read_text(encoding="utf-8", errors="ignore")))
    return "\n".join(parts)


def _selectors(css_text=None):
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

    walk(_without_comments(CSS if css_text is None else css_text))
    return found


def test_no_colour_is_written_out_outside_root():
    literals = re.findall(r"#[0-9a-fA-F]{3,8}\b|rgba?\([^)]*\)|hsla?\([^)]*\)", _outside_root())
    assert literals == []


def test_every_variable_is_used():
    css = _without_comments(CSS)
    defined = set(re.findall(r"(--[\w-]+)\s*:", css))
    assert defined, "no variables found"
    unused = [name for name in sorted(defined) if not re.search(rf"var\(\s*{re.escape(name)}(?![\w-])", css) and name not in _sources()]
    assert unused == []


# --- class contexts -------------------------------------------------------------------------------

_JINJA = r"\{\{.*?\}\}|\{%.*?%\}"
# class="..." / class='...' (the value may hold {{ }} and {% %}, which may hold quotes themselves)
_CLASS_ATTRIBUTE = re.compile(
    rf"""(?<![\w-])class_?\s*=\s*(?:"((?:[^"{{]|\{{(?![{{%])|{_JINJA})*)"|'((?:[^'{{]|\{{(?![{{%])|{_JINJA})*)')""", re.S
)
# element.classList.add("a", "b") and friends
_CLASS_CALL = re.compile(r"""\bclassList\.(?:add|remove|toggle|contains|replace)\(([^)\n]*)\)""")
# element.className = "a b"
_CLASS_ASSIGN = re.compile(r"""\bclassName\s*\+?=\s*(?:"([^"\n]*)"|'([^'\n]*)')""")


def _quoted(text):
    return re.findall(r"""["']([^"'\n]*)["']""", text)


def _class_words(sources):
    """The text of the places where a class is named, and the open slots of class attributes.

    A slot is a `{{ variable }}` standing alone in a class attribute: the class comes from a value
    that is put in there (`class="icon {{ cls }}"`). In `{{ 'a' if b else 'c' }}` only the quoted
    texts count as classes; a `{{ }}` glued to a word (`tone-{{ tone }}`) builds a name: see `_built`.
    """
    pieces, open_slot = [], False
    for found in _CLASS_ATTRIBUTE.finditer(sources):
        value = found.group(1) if found.group(1) is not None else found.group(2)

        def unwrap(code):
            nonlocal open_slot
            before, after = value[: code.start()][-1:], value[code.end() :][:1]
            if re.match(r"[\w-]", before) or re.match(r"[\w-]", after):  # glued to a word: builds a name
                return " "
            literals = _quoted(code.group(0))
            open_slot = open_slot or (code.group(0).startswith("{{") and not literals)
            return " " + " ".join(literals) + " "

        pieces.append(re.sub(_JINJA, unwrap, value, flags=re.S))
    for found in _CLASS_CALL.finditer(sources):
        pieces.extend(_quoted(found.group(1)))
    for found in _CLASS_ASSIGN.finditer(sources):
        pieces.append(found.group(1) if found.group(1) is not None else found.group(2))
    return "\n".join(pieces), open_slot


def _is_value(text, sources):
    """`text` stands alone as a quoted value ("bad", 'bad'), also as a key of a dict."""
    return re.search(rf"""(["']){re.escape(text)}\1""", sources) is not None


def _built(prefix, sources):
    """Is a name put together from `prefix` and something else (`tone-{{ x }}`, `"tone-" + x`, f"tone-{x}")?"""
    return re.search(rf"(?<![\w-]){re.escape(prefix)}(\{{\{{|\{{|['\"] *~|['\"] *\+)", sources) is not None


# class name -> why it stays in the stylesheet although nothing uses it (a finding, to be settled by hand)
KNOWN_DEAD: dict[str, str] = {}


def dead_classes(css, sources):
    """The class names of the rules in `css` that nothing in `sources` uses.

    A name is used when it stands as a word where a class is named (`class="..."`, `classList.add(...)`,
    `className = ...`: not in any other text). Or when it is put together and the missing part is a value
    of its own in the source: `tone-` + `bad` needs a quoted "bad" somewhere, and a class that comes
    from a variable (`class="icon {{ cls }}"`) needs its whole name as a quoted value.
    """
    words, open_slot = _class_words(sources)
    dead = set()
    for selector in _selectors(css):
        for name in re.findall(r"\.([A-Za-z_][\w-]*)", selector):
            if re.search(rf"(?<![\w-]){re.escape(name)}(?![\w-])", words):
                continue
            if open_slot and _is_value(name, sources):
                continue
            prefix, _, suffix = name.rpartition("-")
            if prefix and _built(prefix + "-", sources) and _is_value(suffix, sources):
                continue
            dead.add(name)
    return sorted(dead)


def test_the_code_after_a_comment_stays_in_the_sources():
    python = "# a comment\nx = 'kept'\n\n# another\ny = 'also kept'\n"
    assert _without_source_comments(".py", python).split() == ["x", "=", "'kept'", "y", "=", "'also", "kept'"]
    assert "gone" not in _without_source_comments(".py", 'x = 1\n"""gone"""\ny = 2')


def test_a_class_named_only_in_a_text_is_dead():
    assert dead_classes(".zz-dead { color: red; }", 'text = "this zz-dead text"') == ["zz-dead"]


def test_a_class_in_a_class_attribute_a_script_or_a_class_call_is_used():
    css = ".zz-a { color: red; } .zz-b { color: red; } .zz-c { color: red; } .zz-d { color: red; }"
    sources = "\n".join(
        [
            '<div class="card zz-a">',
            'row.classList.add("zz-b");',
            'row.className = "zz-c";',
            """<span class="x {{ 'zz-d' if y else '' }}">""",
        ]
    )
    assert dead_classes(css, sources) == []


def test_a_built_class_name_needs_its_suffix_as_a_value():
    css = ".tone-zzz { color: red; } .tone-ok { color: red; }"
    built = '<span class="badge tone-{{ tone }}">'
    assert dead_classes(css, built + " tone = 'ok'") == ["tone-zzz"]
    assert dead_classes(css, built + " tone = 'ok'; other = 'zzz'") == []


def test_a_class_that_comes_from_a_variable_needs_its_name_as_a_value():
    css = ".zz-icon { color: red; } .zz-other { color: red; }"
    slot = '<svg class="icon {{ cls }}">'
    assert dead_classes(css, slot) == ["zz-icon", "zz-other"]
    assert dead_classes(css, slot + " call('x', 'zz-icon', 'about zz-other here')") == ["zz-other"]
    # without a slot in any class attribute a value of that name is just a text
    assert dead_classes(css, "call('x', 'zz-icon')") == ["zz-icon", "zz-other"]


def test_the_choices_of_a_built_class_name_are_not_classes_of_their_own():
    css = ".ok { color: red; } .tone-ok { color: red; }"
    sources = """<i class="tone-{{ 'ok' if a else 'warn' }}">"""
    assert dead_classes(css, sources) == ["ok"]


def test_every_class_in_a_rule_is_used_somewhere():
    dead = set(dead_classes(CSS, _sources()))
    assert dead - set(KNOWN_DEAD) == set()


def test_a_known_dead_class_is_still_in_the_stylesheet_and_still_dead():
    """Once a class is used or its rule is removed, its entry goes."""
    dead = set(dead_classes(CSS, _sources()))
    assert set(KNOWN_DEAD) <= dead
