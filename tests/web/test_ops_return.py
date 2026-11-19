"""The way back on the update card (Spec 11.3a "Rückweg zur stabilen Version").

The card offers "Zurück zur stabilen Version X.Y.Z" while a pre-release is installed and no newer stable
version exists, whatever the switch says. The click checks (it loads the stable package, reads its head and
rehearses on a copy) and leads to the confirmation page, which says the case and what is lost and asks the
password again in case 3; the page itself asks no one on the network. Confirming writes the request for
the update helper and starts it. Without the backup from before the pre-release the card says why.
"""

import hashlib
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from beta_steps import beta_scripts
from conftest import PASSWORD, csrf_of
from github_support import FakeGitHub, use_github
from package_support import build_archive

from reprintbuddy.db import make_engine, upgrade
from reprintbuddy.ops.backup import backup_database, backup_dir_for
from reprintbuddy.ops.installation import write_marker
from reprintbuddy.ops.releases import REWORK_LINE, package_name
from reprintbuddy.ops.service import START_UPDATE_CMD
from reprintbuddy.ops.status import UpdateStatus, read_status, write_status

BETA, STABLE = "v1.1.0-beta.1", "v1.0.0"



def sql(path, statement, *args):
    con = sqlite3.connect(path)
    try:
        rows = con.execute(statement, args).fetchall()
        con.commit()
        return rows
    finally:
        con.close()


def add_journal(path, n=1):
    for _ in range(n):
        sql(path, "INSERT INTO journal (at, booked_at, actor, kind, qty, note) VALUES ('2026-11-12 10:00:00', '2026-11-12 10:00:00', 'user', 'stock_in', 1, 'since')")


class Packages:
    """Hands over a real package of each tag (head 0022, as v1.0.0 has it) and its checksum."""

    def __init__(self, folder):
        self.folder = folder
        self.calls: list[str] = []

    def __call__(self, url, dest):
        self.calls.append(url)
        name = url.rsplit("/", 1)[1]
        tag = name.removeprefix("reprintbuddy-").removesuffix(".sha256").removesuffix(".tar.gz")
        archive = self.folder / package_name(tag)
        if not archive.exists():
            self.folder.mkdir(parents=True, exist_ok=True)
            build_archive(archive, tag)
        if name.endswith(".sha256"):
            dest.write_text(f"{hashlib.sha256(archive.read_bytes()).hexdigest()}  {package_name(tag)}\n", encoding="utf-8")
        else:
            dest.write_bytes(archive.read_bytes())


def beta(env, tmp_path, *steps, remote=(BETA, STABLE), backup=True):
    """The installation runs the pre-release; the database stands on `steps` after the stable head."""
    env.install_dir.mkdir(parents=True, exist_ok=True)
    write_marker(env.install_dir, tag=BETA, repo=("owner", "repo"), sha256="0" * 64)
    app = env.client.app
    app.state.installed_tag = BETA
    scripts = beta_scripts(tmp_path, *steps) if steps else None
    if backup:
        backup_database(env.db_path, backup_dir_for(env.db_path), label=STABLE, now=datetime(2026, 11, 10, 9, 30, tzinfo=UTC))
    if scripts is not None:
        engine = make_engine(env.db_path)
        upgrade(engine, scripts)
        engine.dispose()
    packages = Packages(tmp_path / "packages")
    app.state.ops = replace(app.state.ops, download=packages, script_location=scripts)
    use_github(env, FakeGitHub(remote))
    post(env, "/settings/update/check")
    return packages


def post(env, path, **data):
    return env.client.post(path, data={"csrf_token": csrf_of(env.client, "/settings"), **data})


def card(env):
    return env.client.get("/settings?tab=maintenance").text


def checked(env):
    response = post(env, "/settings/update/return/check")
    assert response.status_code == 303
    return response


# --- the button --------------------------------------------------------------------------------------


def test_a_pre_release_without_a_newer_stable_version_offers_the_way_back(ops_env, tmp_path):
    beta(ops_env, tmp_path)
    page = card(ops_env)
    assert "Back to the stable version 1.0.0" in page
    assert 'action="/settings/update/return/check"' in page


def test_a_newer_stable_version_is_the_way_back_instead(ops_env, tmp_path):
    beta(ops_env, tmp_path, remote=("v1.1.0", BETA, STABLE))
    page = card(ops_env)
    assert "Back to the stable version" not in page
    assert "Update to v1.1.0" in page


def test_a_stable_installation_has_no_way_back(ops_env, tmp_path):
    ops_env.install_dir.mkdir(parents=True, exist_ok=True)
    write_marker(ops_env.install_dir, tag=STABLE, repo=("owner", "repo"), sha256="0" * 64)
    use_github(ops_env, FakeGitHub((BETA, STABLE)))
    post(ops_env, "/settings/update/check")
    assert "Back to the stable version" not in card(ops_env)


def test_with_the_switch_on_the_card_offers_both(ops_env, tmp_path):
    from reprintbuddy import settings as st

    with ops_env.client.app.state.sessionmaker() as session:
        st.set_setting(session, "update_channel", "prerelease")
    beta(ops_env, tmp_path, remote=("v1.1.0-beta.2", BETA, STABLE))
    page = card(ops_env)
    assert "Update to v1.1.0-beta.2" in page and "Back to the stable version 1.0.0" in page


# --- the click checks and leads to the confirmation page ------------------------------------------------


def test_case_1_leads_to_a_page_that_keeps_every_entry(ops_env, tmp_path):
    packages = beta(ops_env, tmp_path)
    response = checked(ops_env)
    assert response.headers["location"] == "/settings/update/return"
    page = ops_env.client.get("/settings/update/return").text
    assert "Only the program changes; all data stays." in page
    assert "“Include pre-releases” is off afterwards" in page
    assert 'name="login_password"' not in page.split('id="return-form"')[1].split("</form>")[0]
    assert 'action="/settings/backup"' in page
    assert len(packages.calls) == 2  # package and checksum, once


def test_case_2_names_what_is_lost(ops_env, tmp_path):
    beta(ops_env, tmp_path, "add_table")
    order = sql(ops_env.db_path, "INSERT INTO orders (reference, order_date) VALUES ('A-1', '2026-11-01') RETURNING id")[0][0]
    for text in ("a", "b"):
        sql(ops_env.db_path, "INSERT INTO t_notes (order_id, text) VALUES (?, ?)", order, text)
    checked(ops_env)
    page = ops_env.client.get("/settings/update/return").text
    assert "Lost: Test notes (2 entries)" in page


def test_case_2_without_entries_loses_nothing(ops_env, tmp_path):
    beta(ops_env, tmp_path, "add_table")
    checked(ops_env)
    assert "Nothing is lost." in ops_env.client.get("/settings/update/return").text


def test_case_3_warns_and_lists_the_bookings_since_the_backup(ops_env, tmp_path):
    beta(ops_env, tmp_path, "rework_marked")
    add_journal(ops_env.db_path, 3)
    checked(ops_env)
    page = ops_env.client.get("/settings/update/return").text
    assert "All entries since" in page
    assert "3 journal bookings" in page
    details = page.split('<details class="return-bookings"', 1)[1].split("</details>", 1)[0]
    assert " open" in details.split(">", 1)[0]  # up to ten: open
    assert details.count("<tr>") == 3 + 1  # header row and three bookings
    assert 'name="login_password"' in page


def test_many_bookings_stay_readable(ops_env, tmp_path):
    """Review focus 5: closed, the number in the summary, every booking in the list."""
    beta(ops_env, tmp_path, "rework_marked")
    add_journal(ops_env.db_path, 250)
    checked(ops_env)
    page = ops_env.client.get("/settings/update/return").text
    head, rest = page.split('<details class="return-bookings"', 1)[1].split(">", 1)
    assert " open" not in head
    summary = rest.split("</summary>", 1)[0]
    assert "250" in summary
    assert rest.split("</details>", 1)[0].count("<tr>") == 250 + 1


def test_without_the_backup_the_card_says_why(ops_env, tmp_path):
    beta(ops_env, tmp_path, "rework_marked", backup=False)
    response = checked(ops_env)
    assert response.headers["location"] == "/settings?tab=maintenance&ops=return_no_backup#ops-update"
    assert "no backup from before the pre-release" in ops_env.client.get(response.headers["location"]).text


def test_a_failed_check_says_so_on_the_card(ops_env, tmp_path):
    beta(ops_env, tmp_path)

    def broken(url, dest):
        raise OSError("no network")

    ops_env.client.app.state.ops = replace(ops_env.client.app.state.ops, download=broken)
    response = checked(ops_env)
    assert response.headers["location"] == "/settings?tab=maintenance&ops=return_check_failed#ops-update"


def test_the_page_without_a_check_goes_back_to_the_card(ops_env, tmp_path):
    beta(ops_env, tmp_path)
    response = ops_env.client.get("/settings/update/return", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/settings?tab=maintenance&ops=return_expired#ops-update"


def test_the_page_asks_no_one_on_the_network(ops_env, tmp_path):
    packages = beta(ops_env, tmp_path)
    checked(ops_env)
    github = ops_env.client.app.state.ops.http_get
    asked = len(github.calls)
    ops_env.client.get("/settings/update/return")
    assert len(github.calls) == asked and len(packages.calls) == 2


# --- confirming ---------------------------------------------------------------------------------------


def confirm(env, case, **data):
    token = csrf_of(env.client, "/settings")
    return env.client.post("/settings/update/return", data={"csrf_token": token, "case": str(case), **data})


def test_confirming_case_1_queues_the_way_back_and_starts_the_helper(ops_env, tmp_path):
    beta(ops_env, tmp_path)
    checked(ops_env)
    response = confirm(ops_env, 1)
    assert response.status_code == 303
    status = read_status(ops_env.db_path.parent)
    assert (status.state, status.kind, status.from_tag, status.to_tag) == ("queued", "return", BETA, STABLE)
    assert (status.return_case, status.target_head, status.return_backup) == (1, "0022", None)
    assert START_UPDATE_CMD in ops_env.runner.calls


def test_confirming_case_3_needs_the_password(ops_env, tmp_path):
    beta(ops_env, tmp_path, "rework_marked")
    checked(ops_env)
    page = confirm(ops_env, 3).text
    assert START_UPDATE_CMD not in ops_env.runner.calls
    assert "password" in page.lower()
    confirm(ops_env, 3, login_password="wrong password!")
    assert START_UPDATE_CMD not in ops_env.runner.calls
    response = confirm(ops_env, 3, login_password=PASSWORD)
    assert response.status_code == 303
    status = read_status(ops_env.db_path.parent)
    assert status.return_case == 3 and status.return_backup.endswith(".db")
    assert START_UPDATE_CMD in ops_env.runner.calls


def test_a_check_that_grew_old_is_not_confirmed(ops_env, tmp_path):
    beta(ops_env, tmp_path)
    checked(ops_env)
    plan, at = ops_env.client.app.state.return_prepared
    ops_env.client.app.state.return_prepared = (plan, at - timedelta(minutes=31))
    response = confirm(ops_env, 1)
    assert response.headers["location"] == "/settings?tab=maintenance&ops=return_expired#ops-update"
    assert START_UPDATE_CMD not in ops_env.runner.calls


def test_another_case_than_the_check_found_is_not_confirmed(ops_env, tmp_path):
    beta(ops_env, tmp_path)
    checked(ops_env)
    response = confirm(ops_env, 2)
    assert response.headers["location"] == "/settings?tab=maintenance&ops=return_expired#ops-update"
    assert START_UPDATE_CMD not in ops_env.runner.calls


def test_no_way_back_while_an_update_runs(ops_env, tmp_path):
    beta(ops_env, tmp_path)
    checked(ops_env)
    write_status(ops_env.db_path.parent, UpdateStatus(state="running", step="install", started_at=datetime.now(UTC)))
    response = confirm(ops_env, 1)
    assert response.status_code == 503  # paused like every other change while an update runs
    assert START_UPDATE_CMD not in ops_env.runner.calls


@pytest.mark.parametrize("path", ["/settings/update/return/check", "/settings/update/return"])
def test_the_way_back_needs_the_csrf_token(ops_env, tmp_path, path):
    beta(ops_env, tmp_path)
    response = ops_env.client.post(path, data={"case": "1"})
    assert response.status_code in (400, 403)
    assert START_UPDATE_CMD not in ops_env.runner.calls


def test_the_rework_warning_before_an_update(ops_env, tmp_path):
    """G1: the notes of the offered version begin with the rework line: a warning box, "Update anyway"."""
    from github_support import release_item

    beta(ops_env, tmp_path, remote=(release_item("v1.1.0-beta.2", body=REWORK_LINE + "\n\nNew."), BETA, STABLE))
    from reprintbuddy import settings as st

    with ops_env.client.app.state.sessionmaker() as session:
        st.set_setting(session, "update_channel", "prerelease")
    post(ops_env, "/settings/update/check")
    page = card(ops_env)
    assert "reworks existing data" in page
    assert "Update anyway" in page


# --- the result on the card (Spec 11.3a: "Rückweg fehlgeschlagen, Vorabversion läuft weiter") ---------------


def result_card(env, state, lang="en", **fields):
    from reprintbuddy import settings as st

    with env.client.app.state.sessionmaker() as session:
        st.set_setting(session, "language", lang)
    started = datetime.now(UTC) - timedelta(minutes=5)
    write_status(env.db_path.parent, UpdateStatus(
        state=state, step="done" if state == "succeeded" else "rollback", from_tag=BETA, to_tag=STABLE,
        started_at=started, finished_at=started + timedelta(minutes=3), kind="return", return_case=2, **fields,
    ))
    return card(env)


def test_a_finished_way_back_says_that_the_switch_is_off(ops_env, tmp_path):
    ops_env.install_dir.mkdir(parents=True, exist_ok=True)
    write_marker(ops_env.install_dir, tag=STABLE, repo=("owner", "repo"), sha256="0" * 64)
    page = result_card(ops_env, "succeeded")
    assert "Back on the stable version 1.0.0. “Include pre-releases” is now off." in page
    page = result_card(ops_env, "succeeded", lang="de")
    assert "Zurück auf der stabilen Version 1.0.0. „Vorabversionen einschließen“ ist jetzt aus." in page


@pytest.mark.parametrize(("state", "lang", "words"), [
    ("failed", "de", "Rückweg fehlgeschlagen, Vorabversion läuft weiter"),
    ("rolled_back", "de", "Rückweg fehlgeschlagen, Vorabversion läuft weiter"),
    ("rolled_back", "en", "Way back failed, the pre-release keeps running"),
    ("rollback_failed", "en", "Way back failed and the pre-release could not be restored"),
])
def test_a_failed_way_back_says_so_and_why(ops_env, tmp_path, state, lang, words):
    beta(ops_env, tmp_path)
    page = result_card(ops_env, state, lang=lang, reason="install_failed", params={"detail": "no network"})
    assert words in page
    assert "no network" in page
    assert 'action="/settings/update/dismiss"' in page


# --- review of the whole branch ---------------------------------------------------------------------------


def test_the_page_warns_that_a_backup_without_password_holds_the_keys(ops_env, tmp_path):
    """Spec 11.6: without a password the page says that anyone with the file gets the keys."""
    from reprintbuddy.web.i18n import translate

    beta(ops_env, tmp_path)
    checked(ops_env)
    page = ops_env.client.get("/settings/update/return").text
    warning = translate("en", "ops.backup.warning")
    assert warning.split(".")[0] in page


def test_one_entry_is_said_in_the_singular(ops_env, tmp_path):
    beta(ops_env, tmp_path, "add_table")
    order = sql(ops_env.db_path, "INSERT INTO orders (reference, order_date) VALUES ('A-1', '2026-11-01') RETURNING id")[0][0]
    sql(ops_env.db_path, "INSERT INTO t_notes (order_id, text) VALUES (?, 'a')", order)
    checked(ops_env)
    assert "Lost: Test notes (1 entry)" in ops_env.client.get("/settings/update/return").text


def test_one_booking_is_said_in_the_singular(ops_env, tmp_path):
    beta(ops_env, tmp_path, "rework_marked")
    add_journal(ops_env.db_path, 1)
    checked(ops_env)
    page = ops_env.client.get("/settings/update/return").text
    assert "1 journal booking and" in page
    assert "The 1 journal booking since the backup" in page


def test_two_checks_at_once_do_not_share_their_folder(ops_env, tmp_path, monkeypatch):
    """Review: a double click must not let one check delete the files of the other."""
    from reprintbuddy.web.routes import ops as ops_routes

    beta(ops_env, tmp_path)
    seen = []
    real = ops_routes.prepare_target

    def watch(release, download, work_dir):
        seen.append(work_dir)
        return real(release, download, work_dir)

    monkeypatch.setattr(ops_routes, "prepare_target", watch)
    checked(ops_env)
    checked(ops_env)
    assert len(seen) == 2 and seen[0] != seen[1]
    assert not any(path.exists() for path in seen)
    assert not [p for p in ops_env.db_path.parent.iterdir() if p.name.startswith(".return-check")]


def test_a_stable_version_that_changed_since_the_check_asks_for_a_new_check(ops_env, tmp_path):
    beta(ops_env, tmp_path)
    use_github(ops_env, FakeGitHub((BETA, "v1.0.1", STABLE)))  # the card still knows v1.0.0
    response = checked(ops_env)
    assert response.headers["location"] == "/settings?tab=maintenance&ops=return_none#ops-update"


def test_the_rework_warning_names_the_backup_from_before_the_pre_release():
    from reprintbuddy.web.i18n import translate

    assert "before the switch to the pre-release" in translate("en", "ops.update.rework_warning")
    assert "von vor dem Wechsel auf die Vorabversion" in translate("de", "ops.update.rework_warning")


# --- p610: the start after a way back that was cut off ---------------------------------------------------------


def test_p610_the_pre_release_puts_back_the_database_of_a_cut_off_way_back_before_it_opens_it(tmp_path):
    """A power cut during pip: the live database is the stable version's (an older head, without the newer
    entries). The start of the pre-release must not raise it to its own head first (`upgrade`), but put back
    `.return/before.db`, the database as it was when the service stopped."""
    from reprintbuddy import __version__
    from reprintbuddy.ops.migrationsteps import current_revision
    from reprintbuddy.web.app import create_app

    data = tmp_path / "data"
    data.mkdir()
    db = data / "live.db"
    before = data / ".return" / "before.db"
    before.parent.mkdir()
    source = tmp_path / "pre-release.db"
    engine = make_engine(source)
    upgrade(engine)
    engine.dispose()
    add_journal(source, 3)
    backup_database(source, tmp_path / "copies", label="x", now=datetime(2026, 11, 15, tzinfo=UTC)).rename(before)
    engine = make_engine(db)
    upgrade(engine, revision="0022")  # the stable version's head
    engine.dispose()
    write_status(
        data,
        UpdateStatus(
            state="running", step="install", from_tag="v" + __version__, to_tag=STABLE, kind="return", return_case=3,
            started_at=datetime(2026, 11, 15, tzinfo=UTC), target_head="0022",
        ),
    )
    app = create_app(db, run_engine=False)
    app.state.engine.dispose()
    assert sql(db, "SELECT count(*) FROM journal") == [(3,)]
    assert current_revision(db) == current_revision(source)
    status = read_status(data)
    assert (status.state, status.reason) == ("rolled_back", "interrupted_recovered")
    assert not before.exists()


# --- p611: the backup form of the confirmation page stays on the page -----------------------------------------
# Up to 1.2.0-beta.1 any error of the backup download (a wrong sign-in password above all) led to the update card,
# and the way back had to be checked again. Now the form says where it comes from (`back=return`); the page shows
# the message in its own card "Backup" and keeps the check. Only this one target exists: any other `back` leads to
# the maintenance card as before, never to an address from the request.


def backup_from_the_page(env, **data):
    fields = {"backup_password": "", "backup_password_repeat": "", "login_password": PASSWORD, "back": "return", **data}
    return post(env, "/settings/backup", **fields)


@pytest.mark.parametrize(
    ("fields", "code", "text"),
    [
        ({"login_password": "wrong password!"}, "backup_login", "The sign-in password is wrong. Nothing was downloaded."),
        ({"backup_password": "a" * 12, "backup_password_repeat": "b" * 12}, "backup_mismatch", "The two passwords do not match."),
        ({"backup_password": "short", "backup_password_repeat": "short"}, "backup_length", "The password needs 8 to 1024 characters."),
        ({"broken": True}, "backup_failed", "The backup could not be made. Nothing was downloaded."),
    ],
)
def test_p611_an_error_of_the_backup_form_stays_on_the_confirmation_page(ops_env, tmp_path, monkeypatch, fields, code, text):
    from reprintbuddy.ops import fullbackup

    beta(ops_env, tmp_path, "rework_marked")
    checked(ops_env)
    if fields.pop("broken", False):
        def broken(*args, **kwargs):
            raise OSError("no space left on device")

        monkeypatch.setattr(fullbackup, "create_backup", broken)
    response = backup_from_the_page(ops_env, **fields)
    assert response.status_code == 303
    assert response.headers["location"] == f"/settings/update/return?ops={code}#return-backup"
    page = ops_env.client.get(response.headers["location"])
    assert page.status_code == 200
    backup_card = page.text.split('id="return-backup"')[1].split("</section>")[0]
    assert text in backup_card
    assert 'id="return-confirm"' in page.text  # the check is kept: the page is still the confirmation page
    assert (code == "backup_login") == ('aria-invalid="true"' in backup_card)
    assert backup_card.count(text) == 1


def test_p611_after_the_error_the_way_back_goes_without_a_new_check(ops_env, tmp_path):
    beta(ops_env, tmp_path)
    checked(ops_env)
    backup_from_the_page(ops_env, login_password="wrong password!")
    response = confirm(ops_env, 1)
    assert response.status_code == 303
    assert read_status(ops_env.db_path.parent).kind == "return"
    assert START_UPDATE_CMD in ops_env.runner.calls


def test_p611_the_confirmation_page_passes_over_an_unknown_message(ops_env, tmp_path):
    beta(ops_env, tmp_path)
    checked(ops_env)
    page = ops_env.client.get("/settings/update/return?ops=<script>").text
    backup_card = page.split('id="return-backup"')[1].split("</section>")[0]
    assert 'role="alert"' not in backup_card and "<script>" not in page
    page = ops_env.client.get("/settings/update/return?ops=update_failed").text  # a code of another card
    assert 'role="alert"' not in page.split('id="return-backup"')[1].split("</section>")[0]


@pytest.mark.parametrize("back", ["", "elsewhere", "https://example.com/", "/settings/update/return"])
def test_p611_any_other_origin_leads_to_the_maintenance_card_as_before(ops_env, tmp_path, back):
    beta(ops_env, tmp_path)
    checked(ops_env)
    response = backup_from_the_page(ops_env, login_password="wrong password!", back=back)
    assert response.headers["location"] == "/settings?tab=maintenance&ops=backup_login#ops-backup"


def test_p611_the_backup_form_of_the_page_says_where_it_comes_from(ops_env, tmp_path):
    beta(ops_env, tmp_path)
    checked(ops_env)
    page = ops_env.client.get("/settings/update/return").text
    form = page.split('id="return-backup-form"')[1].split("</form>")[0]
    assert '<input type="hidden" name="back" value="return">' in form


def test_p611_a_check_that_grew_old_meanwhile_leads_to_the_card(ops_env, tmp_path):
    beta(ops_env, tmp_path)
    checked(ops_env)
    plan, at = ops_env.client.app.state.return_prepared
    ops_env.client.app.state.return_prepared = (plan, at - timedelta(minutes=31))
    response = backup_from_the_page(ops_env, login_password="wrong password!")
    follow = ops_env.client.get(response.headers["location"])
    assert follow.status_code == 303 and "return_expired" in follow.headers["location"]
