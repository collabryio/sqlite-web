"""JOIN query builder: schema introspection and safe SQL generation."""

from collections import namedtuple

from sqlite_web.content_query import quote_identifier
from sqlite_web.query_config import (
    ConfigError,
    _require_identifier,
    column_is_indexed,
    key_is_unique)

BASE_ALIAS = 't'
ALLOWED_JOIN_TYPES = frozenset(['LEFT', 'INNER'])
DEFAULT_LIMIT = 100

JoinDef = namedtuple(
    'JoinDef',
    ('table', 'local_alias', 'local_column', 'referenced_column', 'join_type'))
ColumnDef = namedtuple(
    'ColumnDef', ('table_alias', 'column', 'alias'))
JoinRequest = namedtuple(
    'JoinRequest', ('base_table', 'joins', 'columns', 'limit'))


class JoinBuilderError(ValueError):
    pass


def join_alias(index):
    return 'r%d' % index


def get_sorted_tables(dataset):
    virtual_corollary = dataset.get_corollary_virtual_tables()
    return sorted(
        table for table in dataset.tables if table not in virtual_corollary)


def get_table_metadata(dataset, table):
    _require_table(dataset, table)
    columns = []
    for column in dataset.get_columns(table):
        columns.append({
            'name': column.name,
            'data_type': column.data_type,
            'primary_key': column.primary_key,
        })
    foreign_keys = []
    for foreign_key in dataset.get_foreign_keys(table):
        foreign_keys.append({
            'column': foreign_key.column,
            'dest_table': foreign_key.dest_table,
            'dest_column': foreign_key.dest_column,
        })
    return {
        'table': table,
        'columns': columns,
        'foreign_keys': foreign_keys,
    }


def suggest_joins_from_foreign_keys(dataset, base_table, existing_joins=()):
    metadata = get_table_metadata(dataset, base_table)
    suggestions = []
    for index, foreign_key in enumerate(metadata['foreign_keys']):
        suggestions.append({
            'index': index,
            'table': foreign_key['dest_table'],
            'local_alias': BASE_ALIAS,
            'local_column': foreign_key['column'],
            'referenced_column': foreign_key['dest_column'],
            'join_type': 'LEFT',
            'label': '%s.%s -> %s.%s' % (
                base_table,
                foreign_key['column'],
                foreign_key['dest_table'],
                foreign_key['dest_column']),
        })

    for join_index, join in enumerate(existing_joins):
        if join.table not in dataset.tables:
            continue
        joined_metadata = get_table_metadata(dataset, join.table)
        alias = join_alias(join_index)
        for foreign_key in joined_metadata['foreign_keys']:
            suggestions.append({
                'table': foreign_key['dest_table'],
                'local_alias': alias,
                'local_column': foreign_key['column'],
                'referenced_column': foreign_key['dest_column'],
                'join_type': 'LEFT',
                'label': '%s.%s -> %s.%s' % (
                    join.table,
                    foreign_key['column'],
                    foreign_key['dest_table'],
                    foreign_key['dest_column']),
            })
    return suggestions


def _require_table(dataset, table):
    table = _require_identifier(table, 'table')
    if table not in dataset.tables:
        raise JoinBuilderError('Unknown table: %s' % table)
    return table


def _table_for_alias(base_table, joins, alias):
    if alias == BASE_ALIAS:
        return base_table
    if not alias.startswith('r'):
        raise JoinBuilderError('Unknown table alias: %s' % alias)
    try:
        index = int(alias[1:])
    except ValueError:
        raise JoinBuilderError('Unknown table alias: %s' % alias)
    if index < 0 or index >= len(joins):
        raise JoinBuilderError('Unknown table alias: %s' % alias)
    return joins[index].table


def _column_names(dataset, table):
    return {column.name for column in dataset.get_columns(table)}


def parse_join_request(payload):
    if not isinstance(payload, dict):
        raise JoinBuilderError('Join request must be a JSON object.')

    base_table = payload.get('base_table')
    joins_payload = payload.get('joins') or []
    columns_payload = payload.get('columns') or []
    limit = payload.get('limit', DEFAULT_LIMIT)

    if limit is not None and limit != '':
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            raise JoinBuilderError('Limit must be an integer.')
        if limit <= 0:
            raise JoinBuilderError('Limit must be greater than zero.')
    else:
        limit = None

    joins = []
    seen_join_keys = set()
    for item in joins_payload:
        if not isinstance(item, dict):
            raise JoinBuilderError('Each join must be an object.')
        join_type = (item.get('join_type') or 'LEFT').upper()
        if join_type not in ALLOWED_JOIN_TYPES:
            raise JoinBuilderError('Unsupported join type: %s' % join_type)
        join = JoinDef(
            table=_require_identifier(item.get('table'), 'join table'),
            local_alias=_require_identifier(
                item.get('local_alias') or BASE_ALIAS, 'local_alias'),
            local_column=_require_identifier(
                item.get('local_column'), 'local_column'),
            referenced_column=_require_identifier(
                item.get('referenced_column'), 'referenced_column'),
            join_type=join_type)
        join_key = (join.local_alias, join.local_column, join.table)
        if join_key in seen_join_keys:
            raise JoinBuilderError(
                'Duplicate join for %s.%s -> %s' %
                (join.local_alias, join.local_column, join.table))
        seen_join_keys.add(join_key)
        joins.append(join)

    columns = []
    seen_output_aliases = set()
    for item in columns_payload:
        if not isinstance(item, dict):
            raise JoinBuilderError('Each column must be an object.')
        output_alias = item.get('alias')
        if output_alias:
            output_alias = _require_identifier(output_alias, 'column alias')
            if output_alias in seen_output_aliases:
                raise JoinBuilderError(
                    'Duplicate output alias: %s' % output_alias)
            seen_output_aliases.add(output_alias)
        columns.append(ColumnDef(
            table_alias=_require_identifier(
                item.get('table_alias') or BASE_ALIAS, 'table_alias'),
            column=_require_identifier(item.get('column'), 'column'),
            alias=output_alias or None))

    if not columns:
        raise JoinBuilderError('Select at least one output column.')

    return JoinRequest(
        base_table=_require_identifier(base_table, 'base_table'),
        joins=tuple(joins),
        columns=tuple(columns),
        limit=limit)


def validate_join_request(dataset, request):
    base_table = _require_table(dataset, request.base_table)

    alias_tables = {BASE_ALIAS: base_table}
    for index, join in enumerate(request.joins):
        alias = join_alias(index)
        if join.local_alias not in alias_tables:
            raise JoinBuilderError(
                'Join %d references unknown alias %s.' %
                (index, join.local_alias))
        local_table = alias_tables[join.local_alias]
        _require_table(dataset, join.table)

        local_columns = _column_names(dataset, local_table)
        if join.local_column not in local_columns:
            raise JoinBuilderError(
                'Column %s.%s does not exist.' %
                (local_table, join.local_column))

        referenced_columns = _column_names(dataset, join.table)
        if join.referenced_column not in referenced_columns:
            raise JoinBuilderError(
                'Column %s.%s does not exist.' %
                (join.table, join.referenced_column))

        alias_tables[alias] = join.table

    for column in request.columns:
        table = _table_for_alias(base_table, request.joins, column.table_alias)
        if column.column not in _column_names(dataset, table):
            raise JoinBuilderError(
                'Column %s.%s does not exist.' %
                (table, column.column))

    return request


def build_join_warnings(dataset, request):
    warnings = []
    if request.limit is None:
        warnings.append(
            'No LIMIT set. Large tables may scan heavily during preview.')

    base_table = request.base_table
    alias_tables = {BASE_ALIAS: base_table}
    for index, join in enumerate(request.joins):
        alias = join_alias(index)
        local_table = alias_tables[join.local_alias]
        if not column_is_indexed(dataset, local_table, join.local_column):
            warnings.append(
                'Join %s.%s is not indexed; large tables may scan.' %
                (local_table, join.local_column))
        if not key_is_unique(dataset, join.table, join.referenced_column):
            warnings.append(
                'Referenced key %s.%s is not primary/unique indexed.' %
                (join.table, join.referenced_column))
        alias_tables[alias] = join.table
    return warnings


def build_join_sql(request):
    select_parts = []
    for column in request.columns:
        source = '%s.%s' % (
            quote_identifier(column.table_alias),
            quote_identifier(column.column))
        if column.alias:
            select_parts.append(
                '%s AS %s' % (source, quote_identifier(column.alias)))
        else:
            select_parts.append(source)

    sql = 'SELECT %s FROM %s AS %s' % (
        ', '.join(select_parts),
        quote_identifier(request.base_table),
        quote_identifier(BASE_ALIAS))

    for index, join in enumerate(request.joins):
        alias = join_alias(index)
        sql += ' %s JOIN %s AS %s ON %s.%s = %s.%s' % (
            join.join_type,
            quote_identifier(join.table),
            quote_identifier(alias),
            quote_identifier(join.local_alias),
            quote_identifier(join.local_column),
            quote_identifier(alias),
            quote_identifier(join.referenced_column))

    if request.limit is not None:
        sql += ' LIMIT %d' % request.limit
    return sql


def build_join_query(dataset, payload):
    try:
        request = parse_join_request(payload)
    except ConfigError as exc:
        raise JoinBuilderError(str(exc))
    validate_join_request(dataset, request)
    sql = build_join_sql(request)
    warnings = build_join_warnings(dataset, request)
    return {
        'sql': sql,
        'warnings': warnings,
    }
