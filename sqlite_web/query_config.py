"""Saved queries and foreign-key label configuration."""

import re
from collections import namedtuple


SavedQuery = namedtuple('SavedQuery', ('id', 'title', 'description', 'sql'))
ForeignKeyLabel = namedtuple(
    'ForeignKeyLabel',
    ('local_table', 'local_column', 'referenced_table', 'referenced_key',
     'display_column'))

_IDENTIFIER_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')

# Read-only catalog of predefined queries. SQL is resolved server-side by id.
SAVED_QUERIES = (
    SavedQuery(
        id='example_recent_rows',
        title='Example: recent rows',
        description='Sample query template. Replace table name before use.',
        sql='SELECT * FROM "your_table" ORDER BY rowid DESC LIMIT 100'),
)

# Explicit FK -> display-column mappings. Populate for your schema.
# Example:
# ForeignKeyLabel(
#     local_table='orders',
#     local_column='customer_id',
#     referenced_table='customers',
#     referenced_key='id',
#     display_column='name'),
FOREIGN_KEY_LABELS = ()


class ConfigError(ValueError):
    pass


def _require_identifier(value, field_name):
    if not value or not isinstance(value, str):
        raise ConfigError('%s must be a non-empty string.' % field_name)
    if not _IDENTIFIER_RE.match(value):
        raise ConfigError(
            '%s must be a safe SQL identifier: %r' % (field_name, value))
    return value


def validate_saved_queries(queries):
    seen = set()
    validated = []
    for query in queries:
        if not isinstance(query, SavedQuery):
            raise ConfigError('Each saved query must be a SavedQuery record.')
        query_id = _require_identifier(query.id, 'saved query id')
        if query_id in seen:
            raise ConfigError('Duplicate saved query id: %s' % query_id)
        seen.add(query_id)
        if not query.title or not isinstance(query.title, str):
            raise ConfigError('Saved query %s requires a title.' % query_id)
        if not isinstance(query.description, str):
            raise ConfigError(
                'Saved query %s requires a description string.' % query_id)
        if not query.sql or not isinstance(query.sql, str):
            raise ConfigError('Saved query %s requires SQL.' % query_id)
        validated.append(query)
    return tuple(validated)


def validate_foreign_key_labels(labels):
    seen = set()
    validated = []
    for label in labels:
        if not isinstance(label, ForeignKeyLabel):
            raise ConfigError(
                'Each foreign-key label must be a ForeignKeyLabel record.')
        local_table = _require_identifier(label.local_table, 'local_table')
        local_column = _require_identifier(label.local_column, 'local_column')
        referenced_table = _require_identifier(
            label.referenced_table, 'referenced_table')
        referenced_key = _require_identifier(
            label.referenced_key, 'referenced_key')
        display_column = _require_identifier(
            label.display_column, 'display_column')
        key = (local_table, local_column)
        if key in seen:
            raise ConfigError(
                'Duplicate foreign-key label mapping for %s.%s' % key)
        seen.add(key)
        validated.append(ForeignKeyLabel(
            local_table=local_table,
            local_column=local_column,
            referenced_table=referenced_table,
            referenced_key=referenced_key,
            display_column=display_column))
    return tuple(validated)


def validate_query_config(saved_queries=None, foreign_key_labels=None):
    saved_queries = validate_saved_queries(
        saved_queries if saved_queries is not None else SAVED_QUERIES)
    foreign_key_labels = validate_foreign_key_labels(
        foreign_key_labels if foreign_key_labels is not None else
        FOREIGN_KEY_LABELS)
    return saved_queries, foreign_key_labels


def get_saved_query_by_id(query_id, saved_queries):
    for query in saved_queries:
        if query.id == query_id:
            return query
    return None


def get_foreign_key_labels_for_table(table, foreign_key_labels):
    return tuple(
        label for label in foreign_key_labels if label.local_table == table)


def column_is_indexed(dataset, table, column):
    for index in dataset.get_indexes(table):
        if column in index.columns:
            return True
    return False


def key_is_unique(dataset, table, column):
    for col in dataset.get_columns(table):
        if col.name == column and col.primary_key:
            return True
    for index in dataset.get_indexes(table):
        if index.unique and column in index.columns:
            return True
    return False


def validate_foreign_key_label_indexes(dataset, foreign_key_labels):
    warnings = []
    for label in foreign_key_labels:
        if label.local_table not in dataset.tables:
            continue
        if not column_is_indexed(
                dataset, label.local_table, label.local_column):
            warnings.append(
                'Foreign-key label %s.%s: local column is not indexed; '
                'large tables may scan.' %
                (label.local_table, label.local_column))
        if label.referenced_table not in dataset.tables:
            continue
        if not key_is_unique(
                dataset, label.referenced_table, label.referenced_key):
            warnings.append(
                'Foreign-key label %s.%s: referenced key %s.%s is not '
                'unique/primary-key indexed.' %
                (label.local_table, label.local_column,
                 label.referenced_table, label.referenced_key))
    return warnings
