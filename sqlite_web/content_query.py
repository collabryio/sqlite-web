"""Build paginated table-content SQL with optional foreign-key labels."""


def quote_identifier(name):
    return '"%s"' % name.replace('"', '""')


def label_column_alias(local_column):
    return '%s_label' % local_column


def build_labeled_content_sql(table, labels, ordering=None, limit=None,
                              offset=None):
    table_alias = 't'
    select_parts = ['%s.*' % quote_identifier(table_alias)]
    joins = []

    for index, label in enumerate(labels):
        ref_alias = 'r%d' % index
        label_alias = label_column_alias(label.local_column)
        select_parts.append(
            '%s.%s AS %s' % (
                quote_identifier(ref_alias),
                quote_identifier(label.display_column),
                quote_identifier(label_alias)))
        joins.append(
            'LEFT JOIN %s AS %s ON %s.%s = %s.%s' % (
                quote_identifier(label.referenced_table),
                quote_identifier(ref_alias),
                quote_identifier(table_alias),
                quote_identifier(label.local_column),
                quote_identifier(ref_alias),
                quote_identifier(label.referenced_key)))

    sql = 'SELECT %s FROM %s AS %s' % (
        ', '.join(select_parts),
        quote_identifier(table),
        quote_identifier(table_alias))
    if joins:
        sql += ' ' + ' '.join(joins)

    if ordering:
        direction = 'DESC' if ordering.startswith('-') else 'ASC'
        order_column = ordering.lstrip('-')
        sql += ' ORDER BY %s.%s %s' % (
            quote_identifier(table_alias),
            quote_identifier(order_column),
            direction)

    if limit is not None:
        sql += ' LIMIT %d' % int(limit)
        if offset:
            sql += ' OFFSET %d' % int(offset)

    return sql


def fetch_labeled_content_rows(dataset, sql):
    cursor = dataset.query(sql)
    columns = [description[0] for description in cursor.description]
    rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
    return columns, rows
