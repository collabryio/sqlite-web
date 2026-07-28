"""User saved queries persisted beside the database file."""

import datetime
import json
import os
import re
from collections import namedtuple

from sqlite_web.query_config import _require_identifier

UserSavedQuery = namedtuple(
    'UserSavedQuery', ('id', 'title', 'sql', 'created'))

MAX_TITLE_LENGTH = 120
_ID_RE = re.compile(r'^[a-z0-9_]+$')


class UserSavedQueryError(ValueError):
    pass


def get_user_saved_queries_path(dataset):
    db_path = dataset.filename
    base_name = os.path.splitext(os.path.basename(db_path))[0]
    return os.path.join(os.path.dirname(db_path), '%s.saved_queries.json' % base_name)


def _slugify_title(title):
    slug = re.sub(r'[^a-zA-Z0-9]+', '_', title.strip().lower())
    slug = slug.strip('_')
    if not slug:
        raise UserSavedQueryError('Title must contain letters or numbers.')
    if not _ID_RE.match(slug):
        slug = re.sub(r'[^a-z0-9_]', '', slug)
    if not slug:
        raise UserSavedQueryError('Title must contain letters or numbers.')
    return slug


def _validate_title(title):
    if not title or not isinstance(title, str):
        raise UserSavedQueryError('Query name is required.')
    title = title.strip()
    if not title:
        raise UserSavedQueryError('Query name is required.')
    if len(title) > MAX_TITLE_LENGTH:
        raise UserSavedQueryError(
            'Query name must be %d characters or fewer.' % MAX_TITLE_LENGTH)
    return title


def _validate_sql(sql):
    if not sql or not isinstance(sql, str):
        raise UserSavedQueryError('SQL is required.')
    sql = sql.strip()
    if not sql:
        raise UserSavedQueryError('SQL is required.')
    return sql


def load_user_saved_queries(path):
    if not os.path.exists(path):
        return []
    try:
        with open(path, 'r', encoding='utf-8') as fh:
            payload = json.load(fh)
    except (IOError, OSError, ValueError) as exc:
        raise UserSavedQueryError('Unable to read saved queries: %s' % exc)

    if not isinstance(payload, list):
        raise UserSavedQueryError('Saved queries file must contain a list.')

    queries = []
    seen_ids = set()
    for item in payload:
        if not isinstance(item, dict):
            raise UserSavedQueryError('Each saved query must be an object.')
        query_id = _require_identifier(item.get('id'), 'query id')
        if query_id in seen_ids:
            raise UserSavedQueryError('Duplicate saved query id: %s' % query_id)
        seen_ids.add(query_id)
        title = _validate_title(item.get('title'))
        sql = _validate_sql(item.get('sql'))
        created = item.get('created') or ''
        queries.append(UserSavedQuery(
            id=query_id,
            title=title,
            sql=sql,
            created=created))
    return queries


def _write_user_saved_queries(path, queries):
    directory = os.path.dirname(path)
    if directory and not os.path.exists(directory):
        os.makedirs(directory, exist_ok=True)
    payload = [{
        'id': query.id,
        'title': query.title,
        'sql': query.sql,
        'created': query.created,
    } for query in queries]
    tmp_path = '%s.tmp' % path
    try:
        with open(tmp_path, 'w', encoding='utf-8') as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
            fh.write('\n')
        os.replace(tmp_path, path)
    except (IOError, OSError) as exc:
        if os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        raise UserSavedQueryError('Unable to write saved queries: %s' % exc)


def save_user_saved_query(path, title, sql):
    title = _validate_title(title)
    sql = _validate_sql(sql)
    query_id = _slugify_title(title)
    queries = load_user_saved_queries(path)
    created = datetime.datetime.utcnow().replace(microsecond=0).isoformat() + 'Z'

    updated = []
    found = False
    saved = None
    for query in queries:
        if query.id == query_id:
            saved = UserSavedQuery(
                id=query_id,
                title=title,
                sql=sql,
                created=query.created or created)
            updated.append(saved)
            found = True
        else:
            updated.append(query)

    if not found:
        for query in queries:
            if query.title.lower() == title.lower():
                raise UserSavedQueryError(
                    'A saved query with this name already exists.')
        saved = UserSavedQuery(
            id=query_id,
            title=title,
            sql=sql,
            created=created)
        updated.insert(0, saved)
    else:
        if saved is None:
            raise UserSavedQueryError('Unable to save query.')

    _write_user_saved_queries(path, updated)
    return saved


def delete_user_saved_query(path, query_id):
    query_id = _require_identifier(query_id, 'query id')
    queries = load_user_saved_queries(path)
    remaining = [query for query in queries if query.id != query_id]
    if len(remaining) == len(queries):
        raise UserSavedQueryError('Unknown saved query: %s' % query_id)
    if remaining:
        _write_user_saved_queries(path, remaining)
    elif os.path.exists(path):
        os.unlink(path)
    return True


def get_user_saved_query_by_id(path, query_id):
    query_id = _require_identifier(query_id, 'query id')
    for query in load_user_saved_queries(path):
        if query.id == query_id:
            return query
    return None


def serialize_user_saved_query(query):
    return {
        'id': query.id,
        'title': query.title,
        'sql': query.sql,
        'created': query.created,
        'source': 'user',
        'description': 'Custom saved query',
    }


def load_user_saved_queries_for_dataset(dataset):
    path = get_user_saved_queries_path(dataset)
    try:
        queries = load_user_saved_queries(path)
    except UserSavedQueryError:
        queries = []
    return queries, path
