"""SQL 词法切分与首关键字识别。

切分器理解单/双引号、反引号、[方括号]标识符、行/块注释（块注释可嵌套）、

括号深度以及 ``CREATE TRIGGER ... BEGIN ... END`` 触发器体，

因此字符串、注释或触发器体里的分号不会被误当作语句分隔符。

这不是对关键词文本做朴素搜索：所有判定都基于词法状态。

"""

from __future__ import annotations

_IDENT_START = lambda ch: ch.isalpha() or ch == "_"


class TokenizeError(ValueError):
    pass


def _scan_quoted(sql: str, i: int, quote: str) -> int:
    """返回引号区间之后的下标。"""
    n = len(sql)
    i += 1
    if quote == "[":
        end = sql.find("]", i)
        if end == -1:
            raise TokenizeError("unterminated bracketed identifier")
        return end + 1
    while i < n:
        ch = sql[i]
        if ch == quote:
            if i + 1 < n and sql[i + 1] == quote:
                i += 2
                continue
            return i + 1
        i += 1
    raise TokenizeError(f"unterminated {quote} quoted string")


def _scan_block_comment(sql: str, i: int) -> int:
    n = len(sql)
    depth = 1
    i += 2
    while i < n:
        if sql.startswith("/*", i):
            depth += 1
            i += 2
        elif sql.startswith("*/", i):
            depth -= 1
            i += 2
            if depth == 0:
                return i
        else:
            i += 1
    raise TokenizeError("unterminated block comment")


def _scan_word(sql: str, i: int) -> int:
    n = len(sql)
    i += 1
    while i < n and (sql[i].isalnum() or sql[i] in "_$"):
        i += 1
    return i


def split_statements(sql: str) -> list[str]:
    """把一段脚本切成语句列表，空白语句被丢弃。"""
    statements: list[str] = []
    buf: list[str] = []
    n = len(sql)
    i = 0
    depth = 0
    # 当前语句（自上个分号以来）出现过的裸关键字。
    kw: list[str] = []
    trigger_pending = False  # CREATE [TEMP] TRIGGER 已出现，等待 BEGIN
    in_trigger_body = False
    case_depth = 0

    def cut(end: int) -> None:
        text = "".join(buf[:end]).strip()
        del buf[:]
        if text:
            statements.append(text)

    while i < n:
        ch = sql[i]
        if ch in "'\"`":
            j = _scan_quoted(sql, i, ch)
            buf.append(sql[i:j])
            i = j
            continue
        if ch == "[":
            j = _scan_quoted(sql, i, "[")
            buf.append(sql[i:j])
            i = j
            continue
        if sql.startswith("--", i):
            j = sql.find("\n", i)
            j = n if j == -1 else j + 1
            buf.append(sql[i:j])
            i = j
            continue
        if sql.startswith("/*", i):
            j = _scan_block_comment(sql, i)
            buf.append(sql[i:j])
            i = j
            continue
        if ch.isspace():
            buf.append(ch)
            i += 1
            continue
        if not in_trigger_body and ch == "(":
            depth += 1
            buf.append(ch)
            i += 1
            continue
        if not in_trigger_body and ch == ")":
            depth = max(0, depth - 1)
            buf.append(ch)
            i += 1
            continue
        if depth == 0 and ch == ";":
            if not in_trigger_body:
                cut(len(buf))
                kw = []
                trigger_pending = False
                case_depth = 0
            else:
                # 触发器体内的分号只是内部语句分隔，整段触发器作为一条语句。
                buf.append(ch)
            i += 1
            continue
        if _IDENT_START(ch):
            j = _scan_word(sql, i)
            word = sql[i:j]
            buf.append(word)
            upper = word.upper()
            if not in_trigger_body and depth == 0:
                if trigger_pending:
                    if upper == "BEGIN":
                        in_trigger_body = True
                else:
                    kw.append(upper)
                    # CREATE [TEMP|TEMPORARY] TRIGGER
                    if upper == "TRIGGER" and "CREATE" in kw:
                        tail = kw[kw.index("CREATE") + 1:]
                        if tail and tail[0] in ("TEMP", "TEMPORARY"):
                            tail = tail[1:]
                        if tail and tail[0] == "TRIGGER":
                            trigger_pending = True
            elif in_trigger_body:
                if upper == "CASE":
                    case_depth += 1
                elif upper == "END":
                    if case_depth > 0:
                        case_depth -= 1
                    else:
                        cut(len(buf))
                        kw = []
                        trigger_pending = False
                        in_trigger_body = False
                        case_depth = 0
                        i = j
                        while i < n and sql[i].isspace():
                            i += 1
                        if i < n and sql[i] == ";":
                            i += 1
                        continue
            i = j
            continue
        buf.append(ch)
        i += 1

    if in_trigger_body:
        raise TokenizeError("trigger body is missing END")
    cut(len(buf))
    return statements


def first_keyword(statement: str) -> str:
    """返回语句的首个裸关键字（小写），注释和引号被正确跳过。"""
    n = len(statement)
    i = 0
    while i < n:
        ch = statement[i]
        if ch in "'\"`[":
            i = _scan_quoted(statement, i, "[" if ch == "[" else ch)
            continue
        if statement.startswith("--", i):
            j = statement.find("\n", i)
            i = n if j == -1 else j + 1
            continue
        if statement.startswith("/*", i):
            i = _scan_block_comment(statement, i)
            continue
        if _IDENT_START(ch):
            j = _scan_word(statement, i)
            return statement[i:j].lower()
        i += 1
    return ""
