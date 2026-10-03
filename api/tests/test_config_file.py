"""Behavior tests for the bounded literal configuration artifact format."""

from importlib import import_module
import errno
import os
from pathlib import Path
import stat
import subprocess
import sys

import pytest


def test_parses_conventional_assignments_as_literal_values():
    codec = import_module("api.app.config_file")

    values = codec.parse_environment(
        "\n# Hybrid configuration\n"
        " export FAX_BACKEND = phaxio \n"
        "PHAXIO_API_KEY = outbound key  # operator comment\n"
        "SINCH_API_KEY=inbound#key\n"
        "EMPTY=\n",
        allowed_keys={"FAX_BACKEND", "PHAXIO_API_KEY", "SINCH_API_KEY", "EMPTY"},
    )

    assert values == {
        "FAX_BACKEND": "phaxio",
        "PHAXIO_API_KEY": "outbound key",
        "SINCH_API_KEY": "inbound#key",
        "EMPTY": "",
    }


@pytest.mark.parametrize(
    ("assignment", "expected"),
    [
        ("KEY='  literal # \\n ${TOKEN} `tick`  ' # comment", "  literal # \\n ${TOKEN} `tick`  "),
        ('KEY="  quote\\" slash\\\\ newline\\n#${TOKEN}`tick` café  " # comment',
         '  quote" slash\\ newline\n#${TOKEN}`tick` café  '),
        ('KEY="\\u00e9\\ud83d\\udce0"', "é📠"),
        ('KEY="tab\\treturn\\rback\\bform\\fslash\\/"', "tab\treturn\rback\bform\fslash/"),
        ("KEY=''", ""),
        ('KEY=""', ""),
    ],
)
def test_parses_quoted_literal_values(assignment, expected):
    codec = import_module("api.app.config_file")

    assert codec.parse_environment(assignment + "\n", allowed_keys={"KEY"}) == {"KEY": expected}


def test_rejects_duplicate_keys_without_exposing_either_secret():
    codec = import_module("api.app.config_file")

    with pytest.raises(ValueError) as caught:
        codec.parse_environment(
            "KEY=first-secret-sentinel\nKEY=second-secret-sentinel\n",
            allowed_keys={"KEY"},
        )

    error = caught.value
    assert isinstance(error, codec.ConfigurationFileError)
    assert error.line == 2
    assert error.key == "KEY"
    assert error.published is False
    assert "first-secret-sentinel" not in str(error)
    assert "second-secret-sentinel" not in repr(error)


@pytest.mark.parametrize(
    "bad_line",
    [
        "UNSUPPORTED=secret-value-sentinel",
        "1KEY=secret-value-sentinel",
        "KEY.NAME=secret-value-sentinel",
        "KEY-NAME=secret-value-sentinel",
        "raw secret-value-sentinel",
        "export KEY secret-value-sentinel",
    ],
)
def test_rejects_unsupported_or_malformed_assignments_with_safe_line(bad_line):
    codec = import_module("api.app.config_file")

    with pytest.raises(codec.ConfigurationFileError) as caught:
        codec.parse_environment("KEY=valid\n" + bad_line + "\n", allowed_keys={"KEY"})

    assert caught.value.line == 2
    assert caught.value.key is None
    assert caught.value.published is False
    assert "secret-value-sentinel" not in str(caught.value)
    assert bad_line not in repr(caught.value)


@pytest.mark.parametrize(
    "assignment",
    [
        "KEY='secret-value-sentinel",
        'KEY="secret-value-sentinel',
        "KEY='secret-value-sentinel' extra",
        'KEY="secret-value-sentinel" extra',
        'KEY="secret-value-sentinel\\q"',
        'KEY="secret-value-sentinel\\uZZZZ"',
        'KEY="secret-value-sentinel\t"',
        "KEY='secret-value-sentinel\nOTHER=additional-assignment'\n",
        'KEY="secret-value-sentinel\nOTHER=additional-assignment"\n',
    ],
)
def test_rejects_malformed_quotes_on_the_first_physical_line(assignment):
    codec = import_module("api.app.config_file")

    with pytest.raises(codec.ConfigurationFileError) as caught:
        codec.parse_environment(assignment, allowed_keys={"KEY", "OTHER"})

    assert caught.value.line == 1
    assert caught.value.key == "KEY"
    assert caught.value.published is False
    assert "secret-value-sentinel" not in str(caught.value)
    assert "additional-assignment" not in repr(caught.value)
    assert caught.value.__suppress_context__ or caught.value.__context__ is None


def test_parse_enforces_the_utf8_byte_limit_including_multibyte_text():
    codec = import_module("api.app.config_file")
    maximum = 1024 * 1024
    payload = "x" * (maximum - 5)

    assert codec.parse_environment("KEY=" + payload + "\n", allowed_keys={"KEY"})["KEY"] == payload
    for oversized in ("KEY=" + payload + "\n#", "KEY=" + "é" * (maximum // 2) + "\n"):
        with pytest.raises(codec.ConfigurationFileError) as caught:
            codec.parse_environment(oversized, allowed_keys={"KEY"})
        assert caught.value.line is None
        assert caught.value.published is False


@pytest.mark.parametrize("invalid_text", [None, b"KEY=value\n", "KEY=secret-value-sentinel\ud800\n", 'KEY="secret-value-sentinel\\ud800"\n'])
def test_parse_rejects_non_utf8_or_non_text_without_exposing_values(invalid_text):
    codec = import_module("api.app.config_file")

    with pytest.raises(codec.ConfigurationFileError) as caught:
        codec.parse_environment(invalid_text, allowed_keys={"KEY"})

    assert "secret-value-sentinel" not in str(caught.value)
    assert caught.value.published is False
    assert caught.value.__suppress_context__ or caught.value.__context__ is None


def test_shell_expressions_are_literal_without_environment_or_filesystem_side_effects(tmp_path):
    codec = import_module("api.app.config_file")
    marker = tmp_path / "command-must-not-execute"
    literal = f"$(touch {marker}) ${{HOME}} `touch {marker}` $HOME"
    before = dict(os.environ)

    assert codec.parse_environment("KEY=" + literal + "\n", allowed_keys={"KEY"}) == {"KEY": literal}
    assert dict(os.environ) == before
    assert not marker.exists()


def test_format_preserves_hybrid_credentials_in_a_deterministic_literal_form():
    codec = import_module("api.app.config_file")
    values = {
        "SINCH_API_KEY": "inbound#key",
        "PHAXIO_API_KEY": "outbound 'quote' \"$dollar\"\\key\nNEXT=bad",
        "FAX_BACKEND": "phaxio",
        "EMPTY": "",
        "LEADING_SPACE": " leading ",
        "UNICODE": "é📠",
        "COMMAND": "$(echo hi) ${HOME} `tick`",
    }
    expected = (
        'COMMAND="$(echo hi) ${HOME} `tick`"\n'
        'EMPTY=""\n'
        'FAX_BACKEND=phaxio\n'
        'LEADING_SPACE=" leading "\n'
        'PHAXIO_API_KEY="outbound \'quote\' \\"$dollar\\"\\\\key\\nNEXT=bad"\n'
        'SINCH_API_KEY="inbound#key"\n'
        'UNICODE="é📠"\n'
    )

    assert codec.format_environment(values) == expected
    assert codec.format_environment(dict(reversed(list(values.items())))) == expected
    assert codec.parse_environment(expected, allowed_keys=values.keys()) == {
        "COMMAND": "$(echo hi) ${HOME} `tick`",
        "EMPTY": "",
        "FAX_BACKEND": "phaxio",
        "LEADING_SPACE": " leading ",
        "PHAXIO_API_KEY": "outbound 'quote' \"$dollar\"\\key\nNEXT=bad",
        "SINCH_API_KEY": "inbound#key",
        "UNICODE": "é📠",
    }


def test_format_empty_mapping_is_newline_terminated():
    codec = import_module("api.app.config_file")

    assert codec.format_environment({}) == "\n"


@pytest.mark.parametrize(
    "invalid_values",
    [
        None,
        [("KEY", "secret-value-sentinel")],
        {"1KEY": "secret-value-sentinel"},
        {"KEY.NAME": "secret-value-sentinel"},
        {"KEY\nINJECTED": "secret-value-sentinel"},
        {1: "secret-value-sentinel", "KEY": "valid"},
        {"KEY": None},
        {"KEY": 0},
        {"KEY": True},
        {"KEY": "secret-value-sentinel\ud800"},
    ],
)
def test_format_rejects_invalid_keys_values_and_non_mapping_without_leaking_data(invalid_values):
    codec = import_module("api.app.config_file")

    with pytest.raises(codec.ConfigurationFileError) as caught:
        codec.format_environment(invalid_values)

    assert caught.value.published is False
    assert "secret-value-sentinel" not in str(caught.value)
    assert "INJECTED" not in repr(caught.value)
    assert caught.value.__suppress_context__ or caught.value.__context__ is None


def test_format_checks_final_encoded_size_after_escaping():
    codec = import_module("api.app.config_file")
    payload = "x" * (1024 * 1024 - 5)

    assert codec.format_environment({"KEY": payload}) == "KEY=" + payload + "\n"
    for too_large in (payload + "x", "\n" * (1024 * 1024 // 2), "é" * (1024 * 1024 // 2)):
        with pytest.raises(codec.ConfigurationFileError) as caught:
            codec.format_environment({"KEY": too_large})
        assert caught.value.published is False


@pytest.mark.parametrize("as_string", [False, True])
def test_reads_real_utf8_configuration_file(tmp_path, as_string):
    codec = import_module("api.app.config_file")
    artifact = tmp_path / "configuration.env"
    artifact.write_bytes(
        b"# literal recovery artifact\r\n"
        b"export FAX_BACKEND = phaxio\r\n"
        b'PHAXIO_API_KEY=" outbound \\"quote\\"\\nnext "\r\n'
        + "SINCH_API_KEY='inbound#clé'\r\n".encode("utf-8")
    )

    assert codec.read_environment(
        str(artifact) if as_string else artifact,
        allowed_keys={"FAX_BACKEND", "PHAXIO_API_KEY", "SINCH_API_KEY"},
    ) == {
        "FAX_BACKEND": "phaxio",
        "PHAXIO_API_KEY": ' outbound "quote"\nnext ',
        "SINCH_API_KEY": "inbound#clé",
    }


def test_missing_optional_file_is_empty_but_required_file_fails_safely(tmp_path):
    codec = import_module("api.app.config_file")
    artifact = tmp_path / "secret-bearing-path-sentinel.env"

    assert codec.read_environment(artifact, allowed_keys={"KEY"}) == {}
    with pytest.raises(codec.ConfigurationFileError) as caught:
        codec.read_environment(artifact, allowed_keys={"KEY"}, missing_ok=False)

    assert caught.value.published is False
    assert caught.value.line is None
    assert "secret-bearing-path-sentinel" not in str(caught.value)
    assert str(tmp_path) not in repr(caught.value)


@pytest.mark.parametrize(
    "payload",
    [
        b"KEY=secret-value-sentinel\xff\n",
        b"KEY=valid\nUNSUPPORTED=secret-value-sentinel\n",
        b"KEY=valid\nKEY=secret-value-sentinel\n",
        b"KEY=" + b"x" * (1024 * 1024) + b"\n",
    ],
    ids=["invalid-utf8", "unsupported-key", "duplicate", "oversized"],
)
def test_existing_invalid_file_never_returns_a_valid_prefix(tmp_path, payload):
    codec = import_module("api.app.config_file")
    artifact = tmp_path / "secret-bearing-path-sentinel.env"
    artifact.write_bytes(payload)
    before = dict(os.environ)

    with pytest.raises(codec.ConfigurationFileError) as caught:
        codec.read_environment(artifact, allowed_keys={"KEY"})

    assert "secret-value-sentinel" not in str(caught.value)
    assert str(artifact) not in repr(caught.value)
    assert caught.value.published is False
    assert caught.value.__suppress_context__ or caught.value.__context__ is None
    assert dict(os.environ) == before
    assert artifact.read_bytes() == payload


@pytest.mark.parametrize("error_number", [errno.EACCES, errno.EIO])
def test_existing_unreadable_file_fails_with_sanitized_os_diagnostic(tmp_path, monkeypatch, error_number):
    codec = import_module("api.app.config_file")
    artifact = tmp_path / "secret-bearing-path-sentinel.env"
    artifact.write_bytes(b"KEY=old-value\n")
    real_open = os.open

    def failed_open(candidate, flags, *args, **kwargs):
        if os.fspath(candidate) == str(artifact):
            raise OSError(error_number, "secret-value-sentinel", str(artifact))
        return real_open(candidate, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", failed_open)
    with pytest.raises(codec.ConfigurationFileError) as caught:
        codec.read_environment(artifact, allowed_keys={"KEY"})

    assert "secret-value-sentinel" not in str(caught.value)
    assert str(artifact) not in repr(caught.value)
    assert caught.value.line is None
    assert caught.value.published is False
    assert caught.value.__suppress_context__
    assert artifact.read_bytes() == b"KEY=old-value\n"


def test_read_directory_is_an_error_instead_of_empty_configuration(tmp_path):
    codec = import_module("api.app.config_file")

    with pytest.raises(codec.ConfigurationFileError) as caught:
        codec.read_environment(tmp_path, allowed_keys={"KEY"})

    assert caught.value.published is False
    assert str(tmp_path) not in str(caught.value)


def test_reads_all_bytes_when_os_returns_partial_utf8_reads(tmp_path, monkeypatch):
    codec = import_module("api.app.config_file")
    artifact = tmp_path / "configuration.env"
    artifact.write_bytes('KEY="clé📠"\nOTHER=second\n'.encode("utf-8"))
    real_read = os.read

    def partial_read(descriptor, count):
        return real_read(descriptor, min(count, 6))

    monkeypatch.setattr(os, "read", partial_read)

    assert codec.read_environment(artifact, allowed_keys={"KEY", "OTHER"}) == {
        "KEY": "clé📠",
        "OTHER": "second",
    }


def test_invalid_later_assignment_is_detected_after_partial_os_reads(tmp_path, monkeypatch):
    codec = import_module("api.app.config_file")
    artifact = tmp_path / "configuration.env"
    artifact.write_bytes(b"KEY=valid\nKEY=secret-value-sentinel\n")
    real_read = os.read

    def partial_read(descriptor, count):
        return real_read(descriptor, min(count, 6))

    monkeypatch.setattr(os, "read", partial_read)
    with pytest.raises(codec.ConfigurationFileError) as caught:
        codec.read_environment(artifact, allowed_keys={"KEY"})

    assert caught.value.line == 2
    assert caught.value.key == "KEY"


@pytest.mark.parametrize(
    ("assignment", "expected"),
    [
        ("KEY=#literal", "#literal"),
        ("KEY= # comment", ""),
        ("KEY=\t# comment", ""),
        ("KEY=value\t# comment", "value"),
        ("KEY=value#literal", "value#literal"),
        ("KEY=  ' # literal ' # comment", " # literal "),
        ('KEY=" # literal "# comment', " # literal "),
    ],
)
def test_only_whitespace_introduces_an_unquoted_inline_comment(assignment, expected):
    codec = import_module("api.app.config_file")

    assert codec.parse_environment(assignment + "\n", allowed_keys={"KEY"}) == {"KEY": expected}


@pytest.mark.parametrize(
    "assignment",
    [
        "KEY='secret-value-sentinel\rOTHER=hidden'\n",
        'KEY="secret-value-sentinel\rOTHER=hidden"\n',
        "KEY=secret-value-sentinel\rOTHER=hidden\n",
    ],
)
def test_rejects_embedded_physical_carriage_returns(assignment):
    codec = import_module("api.app.config_file")

    with pytest.raises(codec.ConfigurationFileError) as caught:
        codec.parse_environment(assignment, allowed_keys={"KEY", "OTHER"})

    assert caught.value.line == 1
    assert caught.value.key == "KEY"
    assert "secret-value-sentinel" not in str(caught.value)


@pytest.mark.parametrize("existing", [False, True])
def test_publishes_complete_private_file_in_the_destination_directory(tmp_path, monkeypatch, existing):
    codec = import_module("api.app.config_file")
    artifact = tmp_path / "recovery.env"
    if existing:
        artifact.write_bytes(b"FAX_BACKEND=old\n")
        artifact.chmod(0o644)
    real_replace = os.replace
    before = dict(os.environ)

    def publish_temporary(source, destination):
        temporary = Path(source)
        assert temporary.parent == artifact.parent
        assert temporary != artifact
        assert stat.S_IMODE(temporary.stat().st_mode) == 0o600
        assert temporary.read_bytes() == b'FAX_BACKEND=phaxio\nPHAXIO_API_KEY=" outbound\\nsecret "\nSINCH_API_KEY="inbound#key"\n'
        return real_replace(source, destination)

    monkeypatch.setattr(os, "replace", publish_temporary)
    codec.write_environment(
        str(artifact),
        {"SINCH_API_KEY": "inbound#key", "FAX_BACKEND": "phaxio", "PHAXIO_API_KEY": " outbound\nsecret "},
        allowed_keys={"FAX_BACKEND", "PHAXIO_API_KEY", "SINCH_API_KEY"},
    )

    assert artifact.read_bytes() == b'FAX_BACKEND=phaxio\nPHAXIO_API_KEY=" outbound\\nsecret "\nSINCH_API_KEY="inbound#key"\n'
    assert stat.S_IMODE(artifact.stat().st_mode) == 0o600
    assert list(tmp_path.iterdir()) == [artifact]
    assert dict(os.environ) == before


def test_partial_os_writes_publish_all_utf8_bytes(tmp_path, monkeypatch):
    codec = import_module("api.app.config_file")
    artifact = tmp_path / "configuration.env"
    real_write = os.write

    def partial_write(descriptor, data):
        return real_write(descriptor, data[:3])

    monkeypatch.setattr(os, "write", partial_write)
    codec.write_environment(artifact, {"KEY": "clé📠\nsecond"}, allowed_keys={"KEY"})

    assert artifact.read_bytes() == 'KEY="clé📠\\nsecond"\n'.encode("utf-8")
    assert stat.S_IMODE(artifact.stat().st_mode) == 0o600
    assert list(tmp_path.iterdir()) == [artifact]


@pytest.mark.parametrize(
    "candidate",
    [
        {"KEY": "valid", "UNSUPPORTED": "secret-value-sentinel"},
        {"KEY": "valid", "BAD\nKEY": "secret-value-sentinel"},
        {"KEY": "valid", "OTHER": "secret-value-sentinel\ud800"},
        {"KEY": "valid", "OTHER": None},
    ],
)
def test_write_validates_every_input_before_any_destination_access(tmp_path, monkeypatch, candidate):
    codec = import_module("api.app.config_file")
    artifact = tmp_path / "configuration.env"
    artifact.write_bytes(b"KEY=old\n")

    def forbidden_filesystem_access(*args, **kwargs):
        raise AssertionError("Invalid input reached the filesystem")

    monkeypatch.setattr(os, "open", forbidden_filesystem_access)
    monkeypatch.setattr(os, "stat", forbidden_filesystem_access)
    with pytest.raises(codec.ConfigurationFileError) as caught:
        codec.write_environment(artifact, candidate, allowed_keys={"KEY", "OTHER"})

    assert caught.value.published is False
    assert "secret-value-sentinel" not in str(caught.value)
    assert artifact.read_bytes() == b"KEY=old\n"
    assert list(tmp_path.iterdir()) == [artifact]


@pytest.mark.parametrize("dangling", [False, True])
def test_existing_final_symlink_is_refused_without_changing_it_or_its_target(tmp_path, dangling):
    codec = import_module("api.app.config_file")
    target = tmp_path / "secret-bearing-target-sentinel.env"
    if not dangling:
        target.write_bytes(b"KEY=original-target\n")
    artifact = tmp_path / "recovery.env"
    artifact.symlink_to(target.name)

    with pytest.raises(codec.ConfigurationFileError) as caught:
        codec.write_environment(artifact, {"KEY": "candidate"}, allowed_keys={"KEY"})

    assert artifact.is_symlink()
    assert artifact.readlink() == Path(target.name)
    if dangling:
        assert not target.exists()
    else:
        assert target.read_bytes() == b"KEY=original-target\n"
    assert caught.value.published is False
    assert caught.value.line is None
    assert "secret-bearing-target-sentinel" not in str(caught.value)
    assert sorted(item.name for item in tmp_path.iterdir()) == (
        ["recovery.env"] if dangling else ["recovery.env", "secret-bearing-target-sentinel.env"]
    )


@pytest.mark.parametrize("failure_step", ["inspect", "create", "permissions", "write", "file-sync", "replace"])
def test_failure_before_replacement_preserves_original_and_cleans_temporary(tmp_path, monkeypatch, failure_step):
    codec = import_module("api.app.config_file")
    artifact = tmp_path / "secret-bearing-path-sentinel.env"
    artifact.write_bytes(b"KEY=old-secret-sentinel\n")
    artifact.chmod(0o644)

    def failure():
        return OSError(errno.EIO, "raw-os-secret-sentinel", str(artifact))

    if failure_step == "inspect":
        real_stat = os.stat

        def failed_inspection(candidate, *args, **kwargs):
            if os.fspath(candidate) == str(artifact) and kwargs.get("follow_symlinks", True) is False:
                raise failure()
            return real_stat(candidate, *args, **kwargs)

        monkeypatch.setattr(os, "stat", failed_inspection)
    elif failure_step == "create":
        real_open = os.open

        def failed_creation(candidate, flags, *args, **kwargs):
            if Path(candidate).name.startswith(".faxbot-config-"):
                raise failure()
            return real_open(candidate, flags, *args, **kwargs)

        monkeypatch.setattr(os, "open", failed_creation)
    elif failure_step == "permissions":
        def failed_permissions(descriptor, mode):
            raise failure()

        monkeypatch.setattr(os, "fchmod", failed_permissions)
    elif failure_step == "write":
        real_write = os.write

        def failed_write(descriptor, data):
            real_write(descriptor, data[:3])
            raise failure()

        monkeypatch.setattr(os, "write", failed_write)
    elif failure_step == "file-sync":
        def failed_file_sync(descriptor):
            raise failure()

        monkeypatch.setattr(os, "fsync", failed_file_sync)
    else:
        def failed_replace(source, destination):
            assert artifact.read_bytes() == b"KEY=old-secret-sentinel\n"
            assert Path(source).read_bytes() == b"KEY=new-secret-sentinel\n"
            raise failure()

        monkeypatch.setattr(os, "replace", failed_replace)

    with pytest.raises(codec.ConfigurationFileError) as caught:
        codec.write_environment(artifact, {"KEY": "new-secret-sentinel"}, allowed_keys={"KEY"})

    assert caught.value.published is False
    assert caught.value.__suppress_context__
    for secret in ("raw-os-secret-sentinel", "old-secret-sentinel", "new-secret-sentinel", str(artifact)):
        assert secret not in str(caught.value)
        assert secret not in repr(caught.value)
    assert artifact.read_bytes() == b"KEY=old-secret-sentinel\n"
    assert stat.S_IMODE(artifact.stat().st_mode) == 0o644
    assert list(tmp_path.iterdir()) == [artifact]


def test_zero_byte_write_is_a_failure_and_preserves_original(tmp_path, monkeypatch):
    codec = import_module("api.app.config_file")
    artifact = tmp_path / "configuration.env"
    artifact.write_bytes(b"KEY=old\n")
    monkeypatch.setattr(os, "write", lambda descriptor, data: 0)

    with pytest.raises(codec.ConfigurationFileError) as caught:
        codec.write_environment(artifact, {"KEY": "candidate"}, allowed_keys={"KEY"})

    assert caught.value.published is False
    assert artifact.read_bytes() == b"KEY=old\n"
    assert list(tmp_path.iterdir()) == [artifact]


def test_cleanup_failure_does_not_replace_the_sanitized_publication_error(tmp_path, monkeypatch):
    codec = import_module("api.app.config_file")
    artifact = tmp_path / "configuration.env"
    artifact.write_bytes(b"KEY=old\n")
    real_unlink = os.unlink

    def failed_replace(source, destination):
        raise OSError(errno.EIO, "primary-secret-sentinel", str(artifact))

    def failed_cleanup(candidate, *args, **kwargs):
        if Path(candidate).name.startswith(".faxbot-config-"):
            raise OSError(errno.EACCES, "cleanup-secret-sentinel", str(candidate))
        return real_unlink(candidate, *args, **kwargs)

    monkeypatch.setattr(os, "replace", failed_replace)
    monkeypatch.setattr(os, "unlink", failed_cleanup)
    try:
        with pytest.raises(codec.ConfigurationFileError) as caught:
            codec.write_environment(artifact, {"KEY": "candidate"}, allowed_keys={"KEY"})

        assert caught.value.published is False
        assert "primary-secret-sentinel" not in str(caught.value)
        assert "cleanup-secret-sentinel" not in str(caught.value)
        assert artifact.read_bytes() == b"KEY=old\n"
        leftovers = [item for item in tmp_path.iterdir() if item != artifact]
        assert len(leftovers) == 1
        assert stat.S_IMODE(leftovers[0].stat().st_mode) == 0o600
    finally:
        for temporary in tmp_path.glob(".faxbot-config-*"):
            real_unlink(temporary)


def test_write_does_not_create_a_missing_parent_directory(tmp_path):
    codec = import_module("api.app.config_file")
    artifact = tmp_path / "secret-bearing-parent-sentinel" / "configuration.env"

    with pytest.raises(codec.ConfigurationFileError) as caught:
        codec.write_environment(artifact, {"KEY": "candidate"}, allowed_keys={"KEY"})

    assert caught.value.published is False
    assert "secret-bearing-parent-sentinel" not in str(caught.value)
    assert not artifact.parent.exists()
    assert list(tmp_path.iterdir()) == []


def test_file_sync_precedes_replacement_and_directory_sync_follows_it(tmp_path, monkeypatch):
    codec = import_module("api.app.config_file")
    artifact = tmp_path / "configuration.env"
    artifact.write_bytes(b"KEY=old\n")
    real_sync, real_replace = os.fsync, os.replace
    observed = []

    def checked_sync(descriptor):
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            assert artifact.read_bytes() == b"KEY=new\n"
            observed.append("directory")
        else:
            assert artifact.read_bytes() == b"KEY=old\n"
            observed.append("file")
        return real_sync(descriptor)

    def checked_replace(source, destination):
        assert artifact.read_bytes() == b"KEY=old\n"
        observed.append("replace")
        return real_replace(source, destination)

    monkeypatch.setattr(os, "fsync", checked_sync)
    monkeypatch.setattr(os, "replace", checked_replace)
    codec.write_environment(artifact, {"KEY": "new"}, allowed_keys={"KEY"})

    assert artifact.read_bytes() == b"KEY=new\n"
    assert observed == ["file", "replace", "directory"]
    assert list(tmp_path.iterdir()) == [artifact]


@pytest.mark.parametrize("error_number", [errno.EIO, errno.EACCES, errno.EBADF])
def test_directory_sync_failure_reports_published_new_file_without_false_rollback(tmp_path, monkeypatch, error_number):
    codec = import_module("api.app.config_file")
    artifact = tmp_path / "secret-bearing-path-sentinel.env"
    artifact.write_bytes(b"KEY=old-secret-sentinel\n")
    real_sync = os.fsync

    def failed_directory_sync(descriptor):
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            assert artifact.read_bytes() == b"KEY=new-secret-sentinel\n"
            raise OSError(error_number, "raw-os-secret-sentinel", str(artifact))
        return real_sync(descriptor)

    monkeypatch.setattr(os, "fsync", failed_directory_sync)
    with pytest.raises(codec.ConfigurationFileError) as caught:
        codec.write_environment(artifact, {"KEY": "new-secret-sentinel"}, allowed_keys={"KEY"})

    assert caught.value.published is True
    assert caught.value.line is None
    assert caught.value.key is None
    assert caught.value.__suppress_context__
    for secret in ("raw-os-secret-sentinel", "old-secret-sentinel", "new-secret-sentinel", str(artifact)):
        assert secret not in str(caught.value)
    assert artifact.read_bytes() == b"KEY=new-secret-sentinel\n"
    assert stat.S_IMODE(artifact.stat().st_mode) == 0o600
    assert list(tmp_path.iterdir()) == [artifact]


def test_directory_open_failure_is_reported_after_publication(tmp_path, monkeypatch):
    codec = import_module("api.app.config_file")
    artifact = tmp_path / "configuration.env"
    artifact.write_bytes(b"KEY=old\n")
    real_open = os.open

    def failed_directory_open(candidate, flags, *args, **kwargs):
        if os.fspath(candidate) == str(tmp_path):
            raise OSError(errno.EACCES, "raw-os-secret-sentinel", str(tmp_path))
        return real_open(candidate, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", failed_directory_open)
    with pytest.raises(codec.ConfigurationFileError) as caught:
        codec.write_environment(artifact, {"KEY": "new"}, allowed_keys={"KEY"})

    assert caught.value.published is True
    assert "raw-os-secret-sentinel" not in str(caught.value)
    assert str(tmp_path) not in str(caught.value)
    assert artifact.read_bytes() == b"KEY=new\n"
    assert list(tmp_path.iterdir()) == [artifact]


@pytest.mark.parametrize("error_number", sorted({errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP, errno.ENOSYS}))
def test_unsupported_directory_sync_can_complete_publication(tmp_path, monkeypatch, error_number):
    codec = import_module("api.app.config_file")
    artifact = tmp_path / "configuration.env"
    real_sync = os.fsync

    def unsupported_directory_sync(descriptor):
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError(error_number, "not supported")
        return real_sync(descriptor)

    monkeypatch.setattr(os, "fsync", unsupported_directory_sync)
    codec.write_environment(artifact, {"KEY": "new"}, allowed_keys={"KEY"})

    assert artifact.read_bytes() == b"KEY=new\n"
    assert stat.S_IMODE(artifact.stat().st_mode) == 0o600
    assert list(tmp_path.iterdir()) == [artifact]


@pytest.mark.parametrize("error_number", [errno.EINVAL, errno.ENOTSUP])
def test_file_sync_failure_is_never_treated_as_optional(tmp_path, monkeypatch, error_number):
    codec = import_module("api.app.config_file")
    artifact = tmp_path / "configuration.env"
    artifact.write_bytes(b"KEY=old\n")

    def failed_sync(descriptor):
        raise OSError(error_number, "raw-os-secret-sentinel")

    monkeypatch.setattr(os, "fsync", failed_sync)
    with pytest.raises(codec.ConfigurationFileError) as caught:
        codec.write_environment(artifact, {"KEY": "new"}, allowed_keys={"KEY"})

    assert caught.value.published is False
    assert "raw-os-secret-sentinel" not in str(caught.value)
    assert artifact.read_bytes() == b"KEY=old\n"
    assert list(tmp_path.iterdir()) == [artifact]


@pytest.mark.parametrize("dangling", [False, True])
def test_read_refuses_existing_final_symlinks_even_with_missing_ok(tmp_path, dangling):
    codec = import_module("api.app.config_file")
    target = tmp_path / "secret-bearing-target-sentinel.env"
    if not dangling:
        target.write_bytes(b"KEY=account-secret-sentinel\n")
    artifact = tmp_path / "configuration.env"
    artifact.symlink_to(target.name)

    with pytest.raises(codec.ConfigurationFileError) as caught:
        codec.read_environment(artifact, allowed_keys={"KEY"}, missing_ok=True)

    assert caught.value.published is False
    assert "secret-bearing-target-sentinel" not in str(caught.value)
    assert "account-secret-sentinel" not in str(caught.value)
    assert artifact.is_symlink()


@pytest.mark.parametrize("replace_before_open", [False, True])
def test_read_fifo_is_bounded_and_does_not_wait_for_a_writer(tmp_path, replace_before_open):
    artifact = tmp_path / "configuration.env"
    if replace_before_open:
        artifact.write_bytes(b"KEY=old\n")
    else:
        os.mkfifo(artifact, 0o600)
    child = """
import os
from pathlib import Path
import sys
from api.app.config_file import ConfigurationFileError, read_environment
artifact = Path(sys.argv[1])
if sys.argv[2] == 'replace':
    real_open = os.open
    def replace_with_fifo(candidate, flags, *args, **kwargs):
        if os.fspath(candidate) == str(artifact):
            artifact.unlink()
            os.mkfifo(artifact, 0o600)
        return real_open(candidate, flags, *args, **kwargs)
    os.open = replace_with_fifo
try:
    read_environment(artifact, allowed_keys={'KEY'})
except ConfigurationFileError as error:
    assert error.published is False
    assert str(artifact) not in str(error)
else:
    raise SystemExit('FIFO was accepted')
"""

    result = subprocess.run(
        [sys.executable, "-c", child, str(artifact), "replace" if replace_before_open else "fifo"],
        env={**os.environ, "PYTHONPATH": os.pathsep.join(filter(None, (
            str(Path(__file__).resolve().parents[2]), os.environ.get("PYTHONPATH")
        )))},
        capture_output=True,
        text=True,
        timeout=2,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert stat.S_ISFIFO(artifact.stat().st_mode)


def test_read_fstat_failure_is_sanitized_and_closes_the_opened_descriptor(tmp_path, monkeypatch):
    codec = import_module("api.app.config_file")
    artifact = tmp_path / "secret-bearing-path-sentinel.env"
    artifact.write_bytes(b"KEY=account-secret-sentinel\n")
    real_fstat = os.fstat
    inspected_descriptors = []

    def failed_fstat(descriptor):
        inspected_descriptors.append(descriptor)
        raise OSError(errno.EIO, "raw-os-secret-sentinel", str(artifact))

    monkeypatch.setattr(os, "fstat", failed_fstat)
    with pytest.raises(codec.ConfigurationFileError) as caught:
        codec.read_environment(artifact, allowed_keys={"KEY"})

    assert caught.value.published is False
    assert "raw-os-secret-sentinel" not in str(caught.value)
    assert str(artifact) not in str(caught.value)
    assert caught.value.__suppress_context__
    assert len(inspected_descriptors) == 1
    with pytest.raises(OSError) as closed:
        real_fstat(inspected_descriptors[0])
    assert closed.value.errno == errno.EBADF
    assert artifact.read_bytes() == b"KEY=account-secret-sentinel\n"
