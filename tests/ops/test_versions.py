from reprintbuddy.ops.versions import is_newer, is_prerelease_tag, newest_release, parse_version


def test_parse_version():
    assert parse_version("v1.2.3") == (1, 2, 3, 1, 0)
    assert parse_version("v1.2.3-beta.4") == (1, 2, 3, 0, 4)
    for bad in ("v1.2", "1.2.3", "v1.2.3-rc1", "latest", "v1.2.3\n", "v01.2.3x"):
        assert parse_version(bad) is None, bad


def test_release_tags_are_plain_ascii_without_leading_zeros():
    """\\d also matches other scripts' digits; a leading zero would make v01.2.3 and v1.2.3 two names."""
    for bad in ("v١.٠.٠", "v01.2.3", "v1.02.3", "v1.2.03", "v1.2.3 "):
        assert parse_version(bad) is None, bad
    assert parse_version("v0.10.100") == (0, 10, 100, 1, 0)


def test_prerelease_tags_are_strict():
    for bad in ("v1.0.0-beta.0", "v1.0.0-beta.01", "v1.0.0-beta", "v1.0.0-rc.1", "v1.0.0-beta.1 ", "v1.0.0-BETA.1"):
        assert parse_version(bad) is None, bad


def test_version_order():
    tags = ["v1.0.1", "v1.0.1-beta.2", "v1.0.0", "v1.0.1-beta.1", "v1.0.2-beta.1"]
    assert sorted(tags, key=parse_version) == ["v1.0.0", "v1.0.1-beta.1", "v1.0.1-beta.2", "v1.0.1", "v1.0.2-beta.1"]
    assert is_newer("v1.0.1", "v1.0.1-beta.2")
    assert not is_newer("v1.0.1-beta.2", "v1.0.1")
    assert is_newer("v1.0.1-beta.10", "v1.0.1-beta.9")
    assert is_newer("v1.0.1-beta.1", "v1.0.0")


def test_is_prerelease_tag():
    assert is_prerelease_tag("v1.0.1-beta.1")
    assert not is_prerelease_tag("v1.0.1")
    assert not is_prerelease_tag("v1.0.1-beta.0")


def test_newest_release():
    assert newest_release(["v0.9.0", "v0.10.0", "v0.2.1", "junk"]) == "v0.10.0"
    assert newest_release([]) is None
    assert newest_release(["junk", "v1.2"]) is None


def test_is_newer():
    assert is_newer("v0.2.0", "v0.1.9")
    assert not is_newer("v0.2.0", "v0.2.0")
    assert not is_newer("v0.1.0", "v0.2.0")
    assert is_newer("v0.1.0", None)
    assert is_newer("v0.1.0", "feature-branch")  # not a release form: counts as older
