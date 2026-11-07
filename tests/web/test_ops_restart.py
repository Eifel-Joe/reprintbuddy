from conftest import csrf_of

from reprintbuddy import settings as st
from reprintbuddy.ops.runner import CommandResult
from reprintbuddy.ops.service import RESTART_CMD


def post_restart(env, token=None):
    token = token or csrf_of(env.client, "/settings")
    return env.client.post("/settings/restart", data={"csrf_token": token})


def set_port(env, port):
    with env.client.app.state.sessionmaker() as session:
        st.set_setting(session, "port", port)


def test_restart_runs_exact_command(ops_env):
    response = post_restart(ops_env)
    assert ops_env.runner.calls == [RESTART_CMD]
    assert response.status_code == 200
    assert "Restarting" in response.text


def test_restart_requires_csrf(ops_env):
    csrf_of(ops_env.client, "/settings")
    assert ops_env.client.post("/settings/restart", data={}).status_code == 403
    assert (
        ops_env.client.post("/settings/restart", data={"csrf_token": "forged"}).status_code == 403
    )
    assert ops_env.runner.calls == []


def test_restart_requires_login(ops_env):
    token = csrf_of(ops_env.client, "/settings")
    ops_env.client.cookies.clear()
    response = ops_env.client.post("/settings/restart", data={"csrf_token": token})
    assert response.status_code == 403
    assert ops_env.runner.calls == []


def test_restart_failure_reported(ops_env):
    ops_env.runner.on(RESTART_CMD, CommandResult(1, stderr="sudo: a password is required"))
    response = post_restart(ops_env)
    assert response.status_code == 303
    assert response.headers["location"] == "/settings?tab=general&ops=restart_failed#ops-restart"
    page = ops_env.client.get("/settings?tab=general&ops=restart_failed")
    assert "Restart failed" in page.text
    assert "password is required" not in page.text


def test_restart_page_waits_on_same_address(ops_env):
    response = post_restart(ops_env)
    assert 'data-health-url="/healthz"' in response.text
    assert 'data-return-url="/settings"' in response.text
    assert "/static/js/ops.js" in response.text


def test_restart_page_has_a_subtitle(ops_env):  # v0.3.8 p72: like the other pages
    response = post_restart(ops_env)
    assert '<p class="page-sub">Reprintbuddy restarts so that changes to the address or the port take effect.</p>' in response.text


def test_restart_page_subtitle_german(ops_env):
    with ops_env.client.app.state.sessionmaker() as session:
        st.set_setting(session, "language", "de")
    response = post_restart(ops_env)
    assert '<p class="page-sub">Reprintbuddy startet neu, damit Änderungen an Adresse oder Port wirksam werden.</p>' in response.text


def test_restart_page_new_address(ops_env):
    set_port(ops_env, 9090)
    response = post_restart(ops_env)
    assert "http://testserver:9090/settings" in response.text
    assert "restart-wait" not in response.text


def test_settings_shows_running_and_pending(ops_env):
    page = ops_env.client.get("/settings").text
    assert "http://testserver:8080" in page
    assert "take effect after a restart" not in page
    set_port(ops_env, 9090)
    assert "take effect after a restart" in ops_env.client.get("/settings").text


def test_unknown_ops_message_ignored(ops_env):
    for url in ("/settings?ops=foo", "/settings?tab=maintenance&ops=foo", "/settings?tab=general&ops=foo"):
        response = ops_env.client.get(url)
        assert response.status_code == 200
        assert "ops.msg" not in response.text
        assert "Restart failed" not in response.text


def test_ops_messages_have_texts():
    from reprintbuddy.web import i18n
    from reprintbuddy.web.routes.ops import OPS_MESSAGES

    locales = i18n.load_locales()
    for code in OPS_MESSAGES:
        assert f"ops.msg.{code}" in locales["en"], code
        assert f"ops.msg.{code}" in locales["de"], code


def test_restart_command_runs_in_the_thread_pool(ops_env, monkeypatch):
    """`sudo` can take seconds: it must not block the event loop of the one worker."""
    from reprintbuddy.web.routes import ops as routes

    seen = []
    real = routes.run_in_threadpool

    async def spy(function, *args, **kwargs):
        seen.append(function)
        return await real(function, *args, **kwargs)

    monkeypatch.setattr(routes, "run_in_threadpool", spy)
    assert post_restart(ops_env).status_code == 200
    assert routes.request_restart in seen
