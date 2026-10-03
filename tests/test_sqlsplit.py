from app.sqlsplit import first_keyword, split_statements


def test_semicolon_in_string_and_comment():
    stmts = split_statements(
        "INSERT INTO t VALUES ('a;b'); -- one; comment\n"  #
        "/* block; comment */ INSERT INTO t VALUES ('it''s;ok');"
    )
    assert len(stmts) == 2
    assert stmts[0].startswith("INSERT")
    assert stmts[1].endswith(";") is False


def test_trigger_body_with_multiple_statements():
    sql = (
        "CREATE TRIGGER tr AFTER INSERT ON t "
        "BEGIN "
        "INSERT INTO log VALUES ('a;b'); "
        "INSERT INTO log VALUES ('c'); "
        "END; "
        "UPDATE t SET x = 1;"
    )
    stmts = split_statements(sql)
    assert len(stmts) == 2
    assert stmts[0].endswith("END")
    assert first_keyword(stmts[1]) == "update"


def test_blob_and_quoted_identifiers():
    stmts = split_statements(
        'CREATE TABLE t("weird;col" BLOB DEFAULT X\'01\'); INSERT INTO t DEFAULT VALUES;'
    )
    assert len(stmts) == 2


def test_case_end_inside_trigger_does_not_terminate():
    sql = (
        "CREATE TRIGGER tr BEFORE UPDATE ON t "
        "BEGIN "
        "UPDATE a SET v = CASE WHEN x=1 THEN 'one' ELSE 'two' END; "
        "INSERT INTO log VALUES ('done'); "
        "END;"
    )
    stmts = split_statements(sql)
    assert len(stmts) == 1
