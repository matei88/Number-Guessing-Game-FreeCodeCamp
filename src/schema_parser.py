import re
from dataclasses import dataclass, field
from typing import Optional, List, Dict


@dataclass
class Column:
    name: str
    sql_type: str
    base_type: str
    nullable: bool = True
    primary_key: bool = False
    unique: bool = False
    auto_increment: bool = False
    default: Optional[str] = None
    enum_values: List[str] = field(default_factory=list)
    max_length: Optional[int] = None
    check_constraint: Optional[str] = None


@dataclass
class ForeignKey:
    column: str
    ref_table: str
    ref_column: str


@dataclass
class Table:
    name: str
    columns: List[Column] = field(default_factory=list)
    foreign_keys: List[ForeignKey] = field(default_factory=list)
    primary_key_column: Optional[str] = None


def parse_ddl(ddl_text: str) -> Dict[str, Table]:
    # Strip comments
    ddl_text = re.sub(r'--[^\n]*', '', ddl_text)
    ddl_text = re.sub(r'/\*.*?\*/', '', ddl_text, flags=re.DOTALL)

    tables: Dict[str, Table] = {}

    create_pattern = re.compile(
        r'CREATE\s+TABLE\s+[`"\']?(\w+)[`"\']?\s*\((.*?)\)\s*;',
        re.IGNORECASE | re.DOTALL,
    )
    for match in create_pattern.finditer(ddl_text):
        table_name = match.group(1)
        body = match.group(2)
        tables[table_name] = _parse_table_body(table_name, body)

    # ALTER TABLE … ADD CONSTRAINT … FOREIGN KEY
    alter_pattern = re.compile(
        r'ALTER\s+TABLE\s+[`"\']?(\w+)[`"\']?\s+ADD\s+(?:CONSTRAINT\s+\w+\s+)?'
        r'FOREIGN\s+KEY\s*\(([^)]+)\)\s+REFERENCES\s+[`"\']?(\w+)[`"\']?\s*\(([^)]+)\)',
        re.IGNORECASE,
    )
    for match in alter_pattern.finditer(ddl_text):
        tname = match.group(1)
        col = match.group(2).strip().strip('`"\'')
        ref_table = match.group(3)
        ref_col = match.group(4).strip().strip('`"\'')
        if tname in tables:
            existing_cols = {fk.column for fk in tables[tname].foreign_keys}
            if col not in existing_cols:
                tables[tname].foreign_keys.append(ForeignKey(col, ref_table, ref_col))

    return tables


def _split_body(body: str) -> List[str]:
    parts, current, depth = [], [], 0
    for ch in body:
        if ch == '(':
            depth += 1
            current.append(ch)
        elif ch == ')':
            depth -= 1
            current.append(ch)
        elif ch == ',' and depth == 0:
            parts.append(''.join(current).strip())
            current = []
        else:
            current.append(ch)
    if current:
        parts.append(''.join(current).strip())
    return parts


def _parse_table_body(table_name: str, body: str) -> Table:
    table = Table(name=table_name)
    for part in _split_body(body):
        part = part.strip()
        if not part:
            continue
        up = part.upper().lstrip()

        if up.startswith('PRIMARY KEY'):
            m = re.search(r'PRIMARY\s+KEY\s*\(([^)]+)\)', part, re.IGNORECASE)
            if m:
                table.primary_key_column = m.group(1).strip().strip('`"\'')

        elif up.startswith('FOREIGN KEY'):
            m = re.search(
                r'FOREIGN\s+KEY\s*\(([^)]+)\)\s+REFERENCES\s+[`"\']?(\w+)[`"\']?\s*\(([^)]+)\)',
                part, re.IGNORECASE,
            )
            if m:
                table.foreign_keys.append(
                    ForeignKey(
                        m.group(1).strip().strip('`"\''),
                        m.group(2),
                        m.group(3).strip().strip('`"\''),
                    )
                )

        elif re.match(r'(UNIQUE|KEY|INDEX|CONSTRAINT)\b', up):
            pass  # skip index/constraint lines

        else:
            col = _parse_column(part)
            if col:
                if col.primary_key and not table.primary_key_column:
                    table.primary_key_column = col.name
                table.columns.append(col)

    return table


def _parse_column(col_def: str) -> Optional[Column]:
    col_def = col_def.strip()
    if not col_def:
        return None

    name_m = re.match(r'[`"\']?(\w+)[`"\']?\s+', col_def)
    if not name_m:
        return None
    name = name_m.group(1)
    rest = col_def[name_m.end():]

    # Capture full type token including parenthesised args (handles ENUM, DECIMAL, VARCHAR…)
    type_m = re.match(r'(\w+(?:\([^)]*\))?)', rest, re.IGNORECASE)
    if not type_m:
        return None
    sql_type = type_m.group(1)
    base_type = re.sub(r'\(.*', '', sql_type).upper()

    enum_values: List[str] = []
    if base_type == 'ENUM':
        ev_m = re.search(r'ENUM\(([^)]+)\)', col_def, re.IGNORECASE)
        if ev_m:
            enum_values = [v.strip().strip("'\"") for v in ev_m.group(1).split(',')]

    max_length: Optional[int] = None
    if base_type in ('VARCHAR', 'CHAR'):
        lm = re.search(r'\((\d+)', sql_type)
        if lm:
            max_length = int(lm.group(1))

    rest_up = rest.upper()
    nullable = 'NOT NULL' not in rest_up
    primary_key = 'PRIMARY KEY' in rest_up
    unique = 'UNIQUE' in rest_up and 'PRIMARY KEY' not in rest_up
    auto_increment = 'AUTO_INCREMENT' in rest_up

    default: Optional[str] = None
    dm = re.search(r"DEFAULT\s+('([^']*)'|(\S+))", rest, re.IGNORECASE)
    if dm:
        default = dm.group(2) if dm.group(2) is not None else dm.group(3)
        if default and default.upper() in ('NULL', 'CURRENT_TIMESTAMP'):
            default = None

    check_constraint: Optional[str] = None
    cm = re.search(r'CHECK\s*\(([^)]+)\)', rest, re.IGNORECASE)
    if cm:
        check_constraint = cm.group(1)

    return Column(
        name=name,
        sql_type=sql_type,
        base_type=base_type,
        nullable=nullable,
        primary_key=primary_key,
        unique=unique,
        auto_increment=auto_increment,
        default=default,
        enum_values=enum_values,
        max_length=max_length,
        check_constraint=check_constraint,
    )


def topological_sort(tables: Dict[str, Table]) -> List[str]:
    deps: Dict[str, set] = {n: set() for n in tables}
    for name, table in tables.items():
        for fk in table.foreign_keys:
            if fk.ref_table in tables and fk.ref_table != name:
                deps[name].add(fk.ref_table)

    visited: set = set()
    temp: set = set()
    order: List[str] = []

    def visit(n: str) -> None:
        if n in temp or n in visited:
            return
        temp.add(n)
        for dep in list(deps[n]):
            visit(dep)
        temp.discard(n)
        visited.add(n)
        order.append(n)

    for n in tables:
        visit(n)

    return order


def schema_summary(tables: Dict[str, Table]) -> str:
    lines = []
    for name, table in tables.items():
        col_strs = []
        for col in table.columns:
            s = f"  {col.name} {col.sql_type}"
            if not col.nullable:
                s += " NOT NULL"
            if col.primary_key:
                s += " PK"
            if col.enum_values:
                s += f" [{', '.join(col.enum_values)}]"
            col_strs.append(s)
        fk_strs = [f"  FK: {fk.column} → {fk.ref_table}.{fk.ref_column}" for fk in table.foreign_keys]
        lines.append(f"Table: {name}\n" + "\n".join(col_strs + fk_strs))
    return "\n\n".join(lines)
