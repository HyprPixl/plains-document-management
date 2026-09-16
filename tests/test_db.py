"""db.py string->type coercion and SQL literal rendering.

The warehouse returns every cell as a STRING; db._coercer casts back to real
Python types. The load-bearing bug this pins: the string 'false' is truthy in
Python, so without coercion every boolean column read as True (AGENTS.md #2).
"""
import types

import pytest

import db


# ── _coercer: string cell -> Python type ─────────────────────────────────────
def test_coercer_boolean_true_and_false():
    c = db._coercer("BOOLEAN")
    assert c("true") is True
    assert c("false") is False          # the bug: 'false' must NOT be truthy
    assert c(None) is None


def test_coercer_boolean_only_true_string_is_true():
    c = db._coercer("BOOLEAN")
    assert c("TRUE") is False           # exact 'true' match only
    assert c("1") is False


@pytest.mark.parametrize("tn", ["INT", "LONG", "SHORT", "BYTE"])
def test_coercer_integers(tn):
    c = db._coercer(tn)
    assert c("42") == 42
    assert isinstance(c("42"), int)
    assert c(None) is None


@pytest.mark.parametrize("tn", ["FLOAT", "DOUBLE", "DECIMAL"])
def test_coercer_floats(tn):
    c = db._coercer(tn)
    assert c("3.14") == pytest.approx(3.14)
    assert isinstance(c("3.14"), float)
    assert c(None) is None


def test_coercer_strings_pass_through():
    c = db._coercer("STRING")
    assert c("hello") == "hello"
    assert c(None) is None


def test_coercer_reads_enum_like_value_attribute():
    # Real SDK type_name objects expose `.value`; the coercer must read it.
    tn = types.SimpleNamespace(value="BOOLEAN")
    assert db._coercer(tn)("false") is False


# ── query() end-to-end coercion over a faked SDK response ────────────────────
class _FakeStatus:
    def __init__(self):
        from databricks.sdk.service.sql import StatementState
        self.state = StatementState.SUCCEEDED
        self.error = None


def _fake_column(name, type_name):
    return types.SimpleNamespace(name=name, type_name=type_name)


def test_query_coerces_every_column(monkeypatch):
    columns = [
        _fake_column("n", "INT"),
        _fake_column("flag", "BOOLEAN"),
        _fake_column("ratio", "DOUBLE"),
        _fake_column("name", "STRING"),
    ]
    resp = types.SimpleNamespace(
        statement_id="s1",
        status=_FakeStatus(),
        result=types.SimpleNamespace(data_array=[["7", "false", "1.5", "abc"]]),
        manifest=types.SimpleNamespace(schema=types.SimpleNamespace(columns=columns)),
    )
    fake_exec = types.SimpleNamespace(
        execute_statement=lambda **kw: resp,
        get_statement=lambda sid: resp,
    )
    monkeypatch.setattr(db, "_w", types.SimpleNamespace(statement_execution=fake_exec))

    rows = db.query("SELECT n, flag, ratio, name FROM t")
    assert rows == [{"n": 7, "flag": False, "ratio": 1.5, "name": "abc"}]


def test_query_empty_result_returns_empty_list(monkeypatch):
    resp = types.SimpleNamespace(
        statement_id="s1", status=_FakeStatus(),
        result=types.SimpleNamespace(data_array=None), manifest=None)
    fake_exec = types.SimpleNamespace(
        execute_statement=lambda **kw: resp, get_statement=lambda sid: resp)
    monkeypatch.setattr(db, "_w", types.SimpleNamespace(statement_execution=fake_exec))
    assert db.query("SELECT 1 WHERE false") == []


# ── lit(): safe SQL literal rendering ────────────────────────────────────────
def test_lit_none_is_null():
    assert db.lit(None) == "NULL"


def test_lit_booleans():
    assert db.lit(True) == "true"
    assert db.lit(False) == "false"


def test_lit_numbers_unquoted():
    assert db.lit(42) == "42"
    assert db.lit(3.5) == "3.5"


def test_lit_strings_are_quoted_and_escaped():
    assert db.lit("plain") == "'plain'"
    assert db.lit("O'Brien") == "'O''Brien'"          # single-quote doubled
    assert db.lit("back\\slash") == "'back\\\\slash'"  # backslash escaped
