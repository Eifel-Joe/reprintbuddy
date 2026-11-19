"""A small browser for the end-to-end run: it reads a page, fills a form there and sends it, as a person would.

Fields come from the form the page shows (hidden ones too: the CSRF token, one-time form tokens, the upload token
of a restore), so the run goes through the same forms and checks as a click in the browser; only the values a
person would type are given. Cookies are kept, redirects are followed and remembered. Standard library only: it
runs on the Python of the container.
"""

from __future__ import annotations

import http.cookiejar
import json
import secrets
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from html.parser import HTMLParser


class WebError(Exception):
    """A page did not have what the run expected; the message says what and where."""


@dataclass
class Form:
    action: str
    method: str = "get"
    enctype: str = "application/x-www-form-urlencoded"
    form_id: str | None = None
    fields: list[tuple[str, str]] = field(default_factory=list)
    files: list[str] = field(default_factory=list)  # names of file inputs


class _Forms(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.forms: list[Form] = []
        self._open: Form | None = None
        self._outside: list[tuple[str, tuple[str, str] | None, str | None]] = []  # (form id, field, file name)
        self._select: tuple[str, Form | None, str | None] | None = None
        self._select_value: str | None = None
        self._select_first: str | None = None
        self._textarea: tuple[str, Form | None, str | None] | None = None
        self._text: list[str] = []

    def _owner(self, attrs: dict[str, str | None]) -> tuple[Form | None, str | None]:
        if attrs.get("form"):
            return None, attrs["form"]
        return self._open, None

    def _add(self, owner: Form | None, form_id: str | None, item: tuple[str, str]) -> None:
        if owner is not None:
            owner.fields.append(item)
        elif form_id is not None:
            self._outside.append((form_id, item, None))

    def handle_starttag(self, tag: str, attrs_list: list[tuple[str, str | None]]) -> None:
        attrs = dict(attrs_list)
        if tag == "form":
            self._open = Form(
                action=attrs.get("action") or "",
                method=(attrs.get("method") or "get").lower(),
                enctype=attrs.get("enctype") or "application/x-www-form-urlencoded",
                form_id=attrs.get("id"),
            )
            self.forms.append(self._open)
            return
        if tag == "option":  # an option has no name of its own: it belongs to the open select
            if self._select is not None:
                value = attrs.get("value") or ""
                if self._select_first is None:
                    self._select_first = value
                if "selected" in attrs:
                    self._select_value = value
            return
        name = attrs.get("name")
        if not name or "disabled" in attrs:
            return
        owner, form_id = self._owner(attrs)
        if tag == "input":
            kind = (attrs.get("type") or "text").lower()
            if kind in ("submit", "button", "image", "reset"):
                return
            if kind == "file":
                if owner is not None:
                    owner.files.append(name)
                elif form_id is not None:
                    self._outside.append((form_id, None, name))
                return
            if kind in ("checkbox", "radio") and "checked" not in attrs:
                return
            default = "on" if kind in ("checkbox", "radio") else ""
            self._add(owner, form_id, (name, attrs.get("value") or default))
        elif tag == "select":
            self._select = (name, owner, form_id)
            self._select_value = None
            self._select_first = None
        elif tag == "textarea":
            self._textarea = (name, owner, form_id)
            self._text = []

    def handle_data(self, data: str) -> None:
        if self._textarea is not None:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "form":
            self._open = None
        elif tag == "select" and self._select is not None:
            name, owner, form_id = self._select
            value = self._select_value if self._select_value is not None else self._select_first
            if value is not None:
                self._add(owner, form_id, (name, value))
            self._select = None
        elif tag == "textarea" and self._textarea is not None:
            name, owner, form_id = self._textarea
            self._add(owner, form_id, (name, "".join(self._text)))
            self._textarea = None

    def close(self) -> None:
        super().close()
        by_id = {form.form_id: form for form in self.forms if form.form_id}
        for form_id, item, file_name in self._outside:
            form = by_id.get(form_id)
            if form is None:
                continue
            if item is not None:
                form.fields.append(item)
            if file_name is not None:
                form.files.append(file_name)


def forms(html: str) -> list[Form]:
    parser = _Forms()
    parser.feed(html)
    parser.close()
    return parser.forms


@dataclass
class Page:
    status: int
    url: str  # the address after redirects
    body: bytes
    headers: dict[str, str]  # names in lower case
    redirects: list[str] = field(default_factory=list)  # every Location followed, in order

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", "replace")

    def json(self) -> object:
        return json.loads(self.body)

    def form(self, action: str) -> Form:
        """The form that sends to `action` (path, query ignored); exactly one must be there."""
        found = [item for item in forms(self.text) if urllib.parse.urlsplit(item.action).path == action]
        if len(found) != 1:
            actions = sorted({item.action for item in forms(self.text)})
            raise WebError(f"{self.url}: {len(found)} forms send to {action} (forms: {actions})")
        return found[0]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001 - urllib's signature
        return None


class Browser:
    def __init__(self, base: str, *, timeout: float = 120.0, context: object | None = None) -> None:
        self.base = base.rstrip("/")
        self.timeout = timeout
        self.cookies = http.cookiejar.CookieJar()
        handlers: list[object] = [urllib.request.ProxyHandler({}), urllib.request.HTTPCookieProcessor(self.cookies), _NoRedirect()]
        if context is not None:
            handlers.append(urllib.request.HTTPSHandler(context=context))  # type: ignore[arg-type]
        self.opener = urllib.request.build_opener(*handlers)  # type: ignore[arg-type]

    def _url(self, path: str) -> str:
        return path if path.startswith(("http://", "https://")) else self.base + path

    def _once(self, method: str, url: str, body: bytes | None, kind: str | None) -> Page:
        request = urllib.request.Request(url, data=body, method=method)
        if kind:
            request.add_header("Content-Type", kind)
        try:
            with self.opener.open(request, timeout=self.timeout) as answer:
                return Page(answer.status, answer.url, answer.read(), {k.lower(): v for k, v in answer.headers.items()})
        except urllib.error.HTTPError as error:
            return Page(error.code, url, error.read(), {k.lower(): v for k, v in error.headers.items()})

    def request(self, method: str, path: str, body: bytes | None = None, kind: str | None = None, *, follow: bool = True) -> Page:
        url = self._url(path)
        page = self._once(method, url, body, kind)
        seen: list[str] = []
        while follow and page.status in (301, 302, 303, 307, 308) and len(seen) < 10:
            location = urllib.parse.urljoin(url, page.headers.get("location", ""))
            seen.append(location)
            url = location
            page = self._once("GET", url, None, None)
        page.redirects = seen
        return page

    def get(self, path: str, *, follow: bool = True) -> Page:
        return self.request("GET", path, follow=follow)

    def submit(
        self, page: Page, action: str, values: dict[str, str] | None = None,
        files: dict[str, tuple[str, bytes]] | None = None, *, follow: bool = True,
    ) -> Page:
        """Send the form of `page` that goes to `action`, with `values` typed in (they replace what the form holds)."""
        form = page.form(action)
        values = dict(values or {})
        fields = [(name, value) for name, value in form.fields if name not in values]
        fields += list(values.items())
        target = urllib.parse.urljoin(page.url, form.action or page.url)
        if form.method != "post":
            query = urllib.parse.urlencode(fields)
            return self.request("GET", target.split("?")[0] + "?" + query, follow=follow)
        if files or form.enctype == "multipart/form-data":
            body, kind = _multipart(fields, files or {})
        else:
            body, kind = urllib.parse.urlencode(fields).encode(), "application/x-www-form-urlencoded"
        return self.request("POST", target, body, kind, follow=follow)


def _multipart(fields: list[tuple[str, str]], files: dict[str, tuple[str, bytes]]) -> tuple[bytes, str]:
    boundary = "----e2e" + secrets.token_hex(12)
    parts: list[bytes] = []
    for name, value in fields:
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
    for name, (filename, data) in files.items():
        head = (
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n"
        )
        parts.append(head.encode() + data + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"
