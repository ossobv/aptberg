from datetime import timedelta

import pytest

from aptberg import cli
from aptberg.duration import format_duration, parse_duration


def test_parse_duration():
    "Days, hours, minutes, seconds"
    assert parse_duration('7d') == timedelta(days=7)
    assert parse_duration('36h') == timedelta(hours=36)
    assert parse_duration('1.5m') == timedelta(seconds=90)
    for bad in ('7', 'd', '7w', ''):
        with pytest.raises(ValueError, match='not a duration'):
            parse_duration(bad)
    for text in ('7d', '36h', '90m', '45s'):
        assert format_duration(parse_duration(text)) == text


def test_cli_refuses_a_bad_age(capsys):
    "A bad AGE is a usage error, before any config is read"
    for argv in (['gc', '--grace=7w'], ['retire', '--older-than=x']):
        with pytest.raises(SystemExit) as exc:
            cli.main(['-c', '/nonexistent', *argv])
        assert exc.value.code == 2
        assert 'not a duration' in capsys.readouterr().err
