#!/usr/bin/env python

__version__ = '0.7.2'

import base64
import datetime
import hashlib
import importlib
import logging
import math
import operator
import optparse
import os
import re
import sys
import tempfile
import threading
import time
import webbrowser
from collections import namedtuple, OrderedDict
from functools import reduce
from functools import wraps
from getpass import getpass
from io import StringIO
from io import TextIOWrapper
from logging.handlers import WatchedFileHandler
from werkzeug.routing import BaseConverter
from werkzeug.utils import secure_filename


try:
    from flask import (
        Flask, abort, flash, g, jsonify, make_response, redirect,
        render_template, request, session, url_for)
except ImportError:
    raise RuntimeError('Unable to import flask module. Install by running '
                       'pip install flask')
try:
    from markupsafe import Markup, escape
except ImportError:
    raise RuntimeError('Unable to import markupsafe module. Install by running'
                       ' pip install markupsafe')

try:
    from pygments import formatters, highlight, lexers
except ImportError:
    import warnings
    warnings.warn('pygments library not found.', ImportWarning)
    syntax_highlight = lambda data: '<pre>%s</pre>' % data
else:
    def syntax_highlight(data):
        if not data:
            return ''
        lexer = lexers.get_lexer_by_name('sql')
        formatter = formatters.HtmlFormatter(linenos=False)
        return highlight(data, lexer, formatter)

try:
    from peewee import __version__ as _pw_version
    peewee_version = tuple([int(p) for p in _pw_version.split('.')])
except ImportError:
    raise RuntimeError('Unable to import peewee module. Install by running '
                       'pip install peewee')
else:
    if peewee_version < (3, 0, 0):
        raise RuntimeError('Peewee >= 3.0.0 is required. Found version %s. '
                           'Please update by running pip install --update '
                           'peewee' % _pw_version)

from peewee import *
from peewee import IndexMetadata
from peewee import sqlite3
from playhouse.dataset import DataSet
from playhouse.migrate import migrate

from sqlite_web.content_query import (
    build_labeled_content_sql,
    fetch_labeled_content_rows,
    label_column_alias)
from sqlite_web.join_builder import (
    JoinBuilderError,
    DEFAULT_LIMIT,
    build_join_query,
    get_sorted_tables,
    get_table_metadata,
    suggest_joins_from_foreign_keys)
from sqlite_web.user_saved_queries import (
    UserSavedQueryError,
    delete_user_saved_query,
    get_user_saved_queries_path,
    get_user_saved_query_by_id,
    load_user_saved_queries_for_dataset,
    save_user_saved_query,
    serialize_user_saved_query)
from sqlite_web.query_config import (
    ConfigError,
    get_foreign_key_labels_for_table,
    get_saved_query_by_id,
    validate_foreign_key_label_indexes,
    validate_query_config)


CUR_DIR = os.path.realpath(os.path.dirname(__file__))
DEBUG = False

BLOB_AS_BASE64 = False  # Default is hex.
ROWS_PER_PAGE = 50
QUERY_ROWS_PER_PAGE = 1000
TRUNCATE_VALUES = True
SECRET_KEY = 'sqlite-database-browser-0.1.0'

app = Flask(
    __name__,
    static_folder=os.path.join(CUR_DIR, 'static'),
    template_folder=os.path.join(CUR_DIR, 'templates'))
app.config.from_object(__name__)
datasets = {}
dataset_config = {}
SAVED_QUERIES = ()
FOREIGN_KEY_LABELS = ()

#
# Database metadata objects.
#

TriggerMetadata = namedtuple('TriggerMetadata', ('name', 'sql'))

ViewMetadata = namedtuple('ViewMetadata', ('name', 'sql'))

#
# Database helpers.
#

class SqliteDataSet(DataSet):
    @property
    def filename(self):
        db_file = self._database.database
        if db_file.startswith('file:'):
            db_file = db_file[5:]
        return os.path.realpath(db_file.rsplit('?', 1)[0])

    @property
    def basename(self):
        return os.path.basename(self.filename)

    @property
    def is_readonly(self):
        db_file = self._database.database
        return db_file.endswith('?mode=ro')

    @property
    def created(self):
        stat = os.stat(self.filename)
        return datetime.datetime.fromtimestamp(stat.st_ctime)

    @property
    def modified(self):
        stat = os.stat(self.filename)
        return datetime.datetime.fromtimestamp(stat.st_mtime)

    @property
    def size_on_disk(self):
        stat = os.stat(self.filename)
        return stat.st_size

    def get_indexes(self, table):
        return self._database.get_indexes(table)

    def get_all_indexes(self):
        cursor = self.query(
            'SELECT name, sql FROM sqlite_master '
            'WHERE type = ? ORDER BY name',
            ('index',))
        return [IndexMetadata(row[0], row[1], None, None, None)
                for row in cursor.fetchall()]

    def get_columns(self, table):
        return self._database.get_columns(table)

    def get_foreign_keys(self, table):
        return self._database.get_foreign_keys(table)

    def get_triggers(self, table):
        cursor = self.query(
            'SELECT name, sql FROM sqlite_master '
            'WHERE type = ? AND tbl_name = ?',
            ('trigger', table))
        return [TriggerMetadata(*row) for row in cursor.fetchall()]

    def get_all_triggers(self):
        cursor = self.query(
            'SELECT name, sql FROM sqlite_master '
            'WHERE type = ? ORDER BY name',
            ('trigger',))
        return [TriggerMetadata(*row) for row in cursor.fetchall()]

    def get_table_sql(self, table):
        if not table:
            return

        cursor = self.query(
            'SELECT sql FROM sqlite_master '
            'WHERE tbl_name = ? AND type IN (?, ?)',
            [table, 'table', 'view'])
        res = cursor.fetchone()
        if res is not None:
            return res[0]

    def get_view(self, name):
        cursor = self.query(
            'SELECT name, sql FROM sqlite_master '
            'WHERE type = ? AND name = ?', ('view', name))
        res = cursor.fetchone()
        if res is not None:
            return ViewMetadata(*res)

    def get_all_views(self):
        cursor = self.query(
            'SELECT name, sql FROM sqlite_master '
            'WHERE type = ? ORDER BY name',
            ('view',))
        return [ViewMetadata(*row) for row in cursor.fetchall()]

    def get_virtual_tables(self):
        cursor = self.query(
            'SELECT name FROM sqlite_master '
            'WHERE type = ? AND sql LIKE ? '
            'ORDER BY name',
            ('table', 'CREATE VIRTUAL TABLE%'))
        return set([row[0] for row in cursor.fetchall()])

    def get_corollary_virtual_tables(self):
        virtual_tables = self.get_virtual_tables()
        suffixes = ['content', 'docsize', 'segdir', 'segments', 'stat']
        return set(
            '%s_%s' % (virtual_table, suffix) for suffix in suffixes
            for virtual_table in virtual_tables)

    def is_view(self, name):
        cursor = self.query(
            'SELECT name FROM sqlite_master '
            'WHERE type = ? AND name = ?', ('view', name))
        return cursor.fetchone() is not None

    def view_operations(self, name):
        cursor = self.query(
            'SELECT sql FROM sqlite_master WHERE type=? AND tbl_name=?',
            ('trigger', name))
        triggers = [t for t, in cursor.fetchall()]
        rgx = re.compile(r'CREATE\s+TRIGGER.+?\sINSTEAD\s+OF\s+'
                         r'(INSERT|UPDATE|DELETE)\s', re.I)
        operations = set()
        for trigger in triggers:
            operations.update([op.lower() for op in rgx.findall(trigger)])

        return operations

class Base64Converter(BaseConverter):
    def to_python(self, value):
        value = base64.urlsafe_b64decode(value)
        try:
            return value.decode('utf8')
        except UnicodeDecodeError:
            return value

    def to_url(self, value):
        if not isinstance(value, (bytes, str)):
            value = str(value).encode('utf8')
        elif isinstance(value, str):
            value = value.encode('utf8')
        return base64.urlsafe_b64encode(value).decode()

app.url_map.converters['b64'] = Base64Converter

def get_dataset():
    if not hasattr(g, 'dataset'):
        dataset_key = session.get('dataset')
        if dataset_key is None or dataset_key not in datasets:
            dataset_key = list(datasets)[0]
            session['dataset'] = dataset_key
        g.dataset = datasets[dataset_key]
    return g.dataset

def serialize_config_saved_query(query):
    return {
        'id': query.id,
        'title': query.title,
        'description': query.description,
        'source': 'config',
    }

def get_all_saved_queries(dataset):
    queries = [serialize_config_saved_query(query) for query in SAVED_QUERIES]
    user_queries, _ = load_user_saved_queries_for_dataset(dataset)
    queries.extend(serialize_user_saved_query(query) for query in user_queries)
    return queries

def get_user_saved_queries_list(dataset):
    user_queries, _ = load_user_saved_queries_for_dataset(dataset)
    return [serialize_user_saved_query(query) for query in user_queries]

#
# Flask views.
#

@app.route('/')
def index():
    return render_template('index.html', sqlite=sqlite3)

@app.route('/login/', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        if request.form.get('password') == app.config['PASSWORD']:
            session['authorized'] = True
            return redirect(session.get('next_url') or url_for('index'))
        flash('The password you entered is incorrect.', 'danger')
        app.logger.debug('Received incorrect password attempt from %s' %
                         request.remote_addr)
    return render_template('login.html')

@app.route('/logout/', methods=['GET'])
def logout():
    session.pop('authorized', None)
    return redirect(url_for('login'))

@app.route('/select-dataset/', methods=['GET'])
def select_dataset():
    dataset = request.args.get('name')
    if dataset and dataset in datasets:
        session['dataset'] = dataset
    else:
        flash('Unable to load selected database.', 'danger')
    return redirect(url_for('index'))

@app.route('/load/', methods=['GET', 'POST'])
def load():
    enable_load = app.config.get('ENABLE_LOAD')
    enable_filesystem = app.config.get('ENABLE_FILESYSTEM')
    if not (enable_load or enable_filesystem):
        flash('Loading databases at run-time is not supported.', 'warning')
        return redirect(url_for('index'))

    dataset = None
    filename = None
    error = None
    if request.method == 'POST':
        filename = request.form.get('filename')
        try:
            dataset, error = _add_dataset(enable_load, enable_filesystem)
        except ValueError as exc:
            error = str(exc)

        if dataset and not error:
            flash('Successfully loaded database.', 'success')
            return redirect(url_for('index'))

    return render_template(
        'load.html',
        filename=filename,
        error=error)

def _add_dataset(enable_load, enable_filesystem):
    mode = request.form.get('mode')
    if mode == 'upload':
        if not enable_load:
            return None, 'Uploading databases is not allowed.'

        database = request.files.get('database')
        if not database:
            return None, 'Database file is required.'

        if app.config['DB_UPLOAD_DIR']:
            dirname = app.config['DB_UPLOAD_DIR']
            os.makedirs(dirname, exist_ok=True)
        else:
            dirname = tempfile.mkdtemp(prefix='sqlite-web')
        filename = secure_filename(database.filename) or 'database.db'
        path = os.path.join(dirname, filename)
        database.save(path)
    elif mode == 'filesystem':
        if not enable_filesystem:
            return None, 'Loading databases from the filesystem is not allowed.'
        path = request.form.get('filename')
        if not path:
            return None, 'Filename is required.'
        if not os.path.exists(path):
            return None, 'File "%s" not found.' % path
    else:
        return None, 'Error: unrecognized mode "%s".' % mode

    try:
        dataset = initialize_dataset(path)
    except Exception as exc:
        return None, 'Unable to load database: %s' % exc
    else:
        basename = os.path.basename(path)
        datasets[basename] = dataset
        session['dataset'] = basename

    return dataset, None

@app.route('/unload/', methods=['GET', 'POST'])
def unload():
    enable_load = app.config.get('ENABLE_LOAD')
    enable_filesystem = app.config.get('ENABLE_FILESYSTEM')
    if not (enable_load or enable_filesystem):
        flash('Unloading databases is not supported.', 'danger')
        return redirect(url_for('index'))
    if len(datasets) == 1:
        flash('Cannot unload dataset.', 'danger')
        return redirect(url_for('index'))

    if request.method == 'POST':
        dataset = request.form.get('dataset')
        if not dataset or dataset not in datasets:
            flash('Database not found.', 'warning')
            return redirect(url_for('unload'))

        ds = datasets.pop(dataset)
        ds.close()

        current = session.get('dataset')
        if current == dataset:
            session['dataset'] = list(datasets)[0]
        flash('Database "%s" unloaded successfully.' % dataset, 'success')
        return redirect(url_for('index'))
    else:
        dataset = request.args.get('dataset')

    return render_template('unload.html', selected=dataset)

def _resolve_query_sql(sql, saved_query_id, user_saved_query_id=None, dataset=None):
    if user_saved_query_id:
        if dataset is None:
            dataset = get_dataset()
        path = get_user_saved_queries_path(dataset)
        saved_query = get_user_saved_query_by_id(path, user_saved_query_id)
        if saved_query is None:
            return None, 'Unknown saved query: %s' % user_saved_query_id
        return saved_query.sql, None
    if saved_query_id:
        saved_query = get_saved_query_by_id(saved_query_id, SAVED_QUERIES)
        if saved_query is None:
            return None, 'Unknown saved query: %s' % saved_query_id
        return saved_query.sql, None
    return sql, None

def _query_view(template, table=None):
    dataset = get_dataset()
    data = []
    data_description = error = row_count = sql = None
    ordering = None
    pk_index = None
    saved_query_id = request.values.get('saved_query') or ''
    user_saved_query_id = request.values.get('user_saved_query') or ''

    sql = qsql = request.values.get('sql') or ''
    if saved_query_id or user_saved_query_id:
        sql, lookup_error = _resolve_query_sql(
            '',
            saved_query_id,
            user_saved_query_id=user_saved_query_id,
            dataset=dataset)
        if lookup_error:
            error = lookup_error
            sql = qsql = ''
        else:
            qsql = sql

    if 'export_json' in request.values:
        ordering = request.values.get('export_ordering')
        export_format = 'json'
    elif 'export_csv' in request.values:
        ordering = request.values.get('export_ordering')
        export_format = 'csv'
    else:
        ordering = request.values.get('ordering')
        export_format = None

    if ordering:
        ordering = int(ordering)
        direction = 'DESC' if ordering < 0 else 'ASC'
        qsql = ('SELECT * FROM (%s) AS _ ORDER BY %d %s' %
                (sql.rstrip(' ;'), abs(ordering), direction))
    else:
        ordering = None

    if table:
        default_sql = 'SELECT * FROM "%s"' % table
        model_class = dataset[table].model_class
        pk = model_class._meta.primary_key
        is_composite_pk = isinstance(pk, CompositeKey)
        allow_edit = not dataset.is_readonly and pk is not False
        allow_bulk = allow_edit and not is_composite_pk
    else:
        default_sql = ''
        model_class = dataset._base_model
        pk = None
        is_composite_pk = False
        allow_edit = False
        allow_bulk = False

    if request.method == 'POST':
        action = request.form.get('action')
        pks = request.form.getlist('pk')
        if action == 'bulk-delete':
            if not allow_bulk:
                flash('Cannot perform bulk operation on this table.', 'warning')
            elif not pks:
                flash('No rows were selected.', 'warning')
            else:
                n = (model_class.delete()
                     .where(model_class._meta.primary_key.in_(pks))
                     .execute())
                flash('Successfully deleted %s row(s)' % n, 'success')

    page = page_next = page_prev = page_start = page_end = total_pages = 1
    total = -1  # Number of rows in query result.
    if qsql:
        if export_format:
            query = model_class.raw(qsql).dicts()
            return export(query, export_format, table)

        try:
            total, = dataset.query('SELECT COUNT(*) FROM (%s) as _' %
                                   qsql.rstrip('; ')).fetchone()
        except Exception as exc:
            total = -1

        # Apply pagination.
        rpp = app.config['QUERY_ROWS_PER_PAGE']
        page = request.values.get('page')
        if page and page.isdigit():
            page = max(int(page), 1)
        else:
            page = 1
        offset = (page - 1) * rpp
        page_prev, page_next = max(page - 1, 1), page + 1

        # Figure out highest page.
        if total > 0:
            total_pages = max(1, int(math.ceil(total / float(rpp))))
            page = max(min(page, total_pages), 1)
            page_next = min(page + 1, total_pages)
            page_start = offset + 1
            page_end = min(total, page_start + rpp - 1)

        qsql = ('SELECT * FROM (%s) AS _ LIMIT %d OFFSET %d' %
                (qsql.rstrip(' ;'), rpp, offset))

        try:
            cursor = dataset.query(qsql)
        except Exception as exc:
            error = str(exc)
            app.logger.exception('Error in user-submitted query.')
        else:
            data = cursor.fetchmany(rpp)
            data_description = cursor.description
            row_count = cursor.rowcount

    if data_description is not None and table:
        col_names = [r[0] for r in data_description]
        if pk and not is_composite_pk and pk.column_name in col_names:
            pk_index = col_names.index(pk.column_name)

    return render_template(
        template,
        allow_bulk=allow_bulk,
        allow_edit=allow_edit,
        data=data,
        data_description=data_description,
        default_sql=default_sql,
        error=error,
        ordering=ordering,
        page=page,
        page_end=page_end,
        page_next=page_next,
        page_prev=page_prev,
        page_start=page_start,
        pk_index=pk_index,
        query_images=get_query_images(),
        row_count=row_count,
        saved_query_id=saved_query_id or None,
        saved_queries=SAVED_QUERIES,
        user_saved_query_id=user_saved_query_id or None,
        sql=sql,
        table=table,
        table_sql=dataset.get_table_sql(table),
        total=total,
        total_pages=total_pages)

@app.route('/query/', methods=['GET', 'POST'])
def generic_query():
    return _query_view('query.html')

@app.route('/saved-queries/', methods=['GET'])
def saved_queries():
    dataset = get_dataset()
    return render_template(
        'saved_queries.html',
        saved_queries=SAVED_QUERIES,
        user_saved_queries=get_user_saved_queries_list(dataset))

@app.route('/saved-queries/list/', methods=['GET'])
def saved_queries_list():
    dataset = get_dataset()
    queries = []
    for query in get_all_saved_queries(dataset):
        item = dict(query)
        if query['source'] == 'user':
            item['run_url'] = url_for(
                'generic_query', user_saved_query=query['id'])
        else:
            item['run_url'] = url_for(
                'generic_query', saved_query=query['id'])
        queries.append(item)
    return jsonify({'queries': queries})

@app.route('/saved-queries/user/', methods=['GET', 'POST'])
def saved_queries_user():
    dataset = get_dataset()
    path = get_user_saved_queries_path(dataset)
    if request.method == 'GET':
        user_queries, _ = load_user_saved_queries_for_dataset(dataset)
        return jsonify({
            'queries': [serialize_user_saved_query(query) for query in user_queries],
        })

    payload = request.get_json(silent=True) or {}
    name = payload.get('name') or request.form.get('name')
    sql = payload.get('sql') or request.form.get('sql')
    try:
        saved = save_user_saved_query(path, name, sql)
    except UserSavedQueryError as exc:
        return jsonify({'error': str(exc)}), 400
    return jsonify({'query': serialize_user_saved_query(saved)})

@app.route('/saved-queries/user/<query_id>/', methods=['DELETE'])
def saved_queries_user_delete(query_id):
    dataset = get_dataset()
    path = get_user_saved_queries_path(dataset)
    try:
        delete_user_saved_query(path, query_id)
    except UserSavedQueryError as exc:
        return jsonify({'error': str(exc)}), 400
    return jsonify({'ok': True})

def require_table(fn):
    @wraps(fn)
    def inner(table, *args, **kwargs):
        if table not in get_dataset().tables:
            abort(404)
        return fn(table, *args, **kwargs)
    return inner

@app.route('/join-builder/tables/', methods=['GET'])
def join_builder_tables():
    dataset = get_dataset()
    return jsonify({'tables': get_sorted_tables(dataset)})

@app.route('/join-builder/table/<table>/', methods=['GET'])
@require_table
def join_builder_table(table):
    dataset = get_dataset()
    return jsonify(get_table_metadata(dataset, table))

@app.route('/join-builder/suggestions/', methods=['POST'])
def join_builder_suggestions():
    dataset = get_dataset()
    payload = request.get_json(silent=True) or {}
    base_table = payload.get('base_table')
    joins_payload = payload.get('joins') or []
    if not base_table or base_table not in dataset.tables:
        return jsonify({'error': 'Unknown base table.'}), 400
    try:
        from sqlite_web.join_builder import parse_join_request, validate_join_request
        columns = dataset.get_columns(base_table)
        if not columns:
            return jsonify({'error': 'Base table has no columns.'}), 400
        request_stub = parse_join_request({
            'base_table': base_table,
            'joins': joins_payload,
            'columns': [{'table_alias': 't', 'column': columns[0].name}],
            'limit': DEFAULT_LIMIT,
        })
        validate_join_request(dataset, request_stub)
        suggestions = suggest_joins_from_foreign_keys(
            dataset, base_table, request_stub.joins)
    except JoinBuilderError as exc:
        return jsonify({'error': str(exc)}), 400
    return jsonify({'suggestions': suggestions})

@app.route('/join-builder/build/', methods=['POST'])
def join_builder_build():
    dataset = get_dataset()
    payload = request.get_json(silent=True) or {}
    try:
        result = build_join_query(dataset, payload)
    except JoinBuilderError as exc:
        return jsonify({'error': str(exc)}), 400
    return jsonify(result)

@app.route('/create-table/', methods=['POST'])
def table_create():
    table = (request.form.get('table_name') or '').strip()
    if not table:
        flash('Table name is required.', 'danger')
        dest = request.form.get('redirect') or url_for('index')
        dest = '/' + dest.lstrip('/')  # idiot vulnerability "researchers".
        return redirect(dest)

    try:
        get_dataset()[table]
    except Exception as exc:
        flash('Error: %s' % str(exc), 'danger')
        app.logger.exception('Error attempting to create table.')
    return redirect(url_for('table_import', table=table))

@app.route('/<table>/')
@require_table
def table_structure(table):
    dataset = get_dataset()
    ds_table = dataset[table]
    model_class = ds_table.model_class

    return render_template(
        'table_structure.html',
        columns=dataset.get_columns(table),
        ds_table=ds_table,
        foreign_keys=dataset.get_foreign_keys(table),
        indexes=dataset.get_indexes(table),
        model_class=model_class,
        table=table,
        table_sql=dataset.get_table_sql(table),
        triggers=dataset.get_triggers(table))

def get_request_data():
    if request.method == 'POST':
        return request.form
    return request.args

@app.route('/<table>/add-column/', methods=['GET', 'POST'])
@require_table
def add_column(table):
    dataset = get_dataset()

    class JsonField(TextField):
        field_type = 'JSON'
    column_mapping = OrderedDict((
        ('TEXT', TextField),
        ('INTEGER', IntegerField),
        ('REAL', FloatField),
        ('BLOB', BlobField),
        ('JSON', JsonField),
        ('BOOL', BooleanField),
        ('DATETIME', DateTimeField),
        ('DATE', DateField),
        ('DECIMAL', DecimalField),
        ('TIME', TimeField),
        ('VARCHAR', CharField)))

    request_data = get_request_data()
    col_type = request_data.get('type')
    name = request_data.get('name', '')

    if request.method == 'POST':
        name = re.sub(r'[^\w]+', '_', name.strip())
        if name and col_type in column_mapping:
            try:
                migrate(
                    dataset._migrator.add_column(
                        table,
                        name,
                        column_mapping[col_type](null=True)))
            except Exception as exc:
                flash('Error attempting to add column "%s": %s' % (name, exc),
                      'danger')
                app.logger.exception('Error attempting to add column.')
            else:
                flash('Column "%s" was added successfully!' % name, 'success')
                dataset.update_cache(table)
                return redirect(url_for('table_structure', table=table))
        else:
            flash('Name and column type are required.', 'danger')

    return render_template(
        'add_column.html',
        col_type=col_type,
        column_mapping=column_mapping,
        name=name,
        table=table,
        table_sql=dataset.get_table_sql(table))

@app.route('/<table>/drop-column/', methods=['GET', 'POST'])
@require_table
def drop_column(table):
    dataset = get_dataset()
    request_data = get_request_data()
    name = request_data.get('name', '')
    columns = dataset.get_columns(table)
    column_names = [column.name for column in columns]

    if request.method == 'POST':
        if name in column_names:
            try:
                migrate(dataset._migrator.drop_column(table, name))
            except Exception as exc:
                flash('Error attempting to drop column "%s": %s' % (name, exc),
                      'danger')
                app.logger.exception('Error attempting to drop column.')
            else:
                flash('Column "%s" was dropped successfully!' % name, 'success')
                dataset.update_cache(table)
                return redirect(url_for('table_structure', table=table))
        else:
            flash('Name is required.', 'danger')

    return render_template(
        'drop_column.html',
        columns=columns,
        column_names=column_names,
        name=name,
        table=table)

@app.route('/<table>/rename-column/', methods=['GET', 'POST'])
@require_table
def rename_column(table):
    dataset = get_dataset()
    request_data = get_request_data()
    rename = request_data.get('rename', '')
    rename_to = request_data.get('rename_to', '')

    columns = dataset.get_columns(table)
    column_names = [column.name for column in columns]

    if request.method == 'POST':
        rename_to = re.sub(r'[^\w]+', '_', rename_to.strip())
        if (rename in column_names) and (rename_to not in column_names):
            try:
                migrate(dataset._migrator.rename_column(table, rename, rename_to))
            except Exception as exc:
                flash('Error attempting to rename column "%s": %s' %
                      (rename, exc), 'danger')
                app.logger.exception('Error attempting to rename column.')
            else:
                flash('Column "%s" was renamed successfully!' % rename, 'success')
                dataset.update_cache(table)
                return redirect(url_for('table_structure', table=table))
        else:
            flash('Column name is required and cannot conflict with an '
                  'existing column\'s name.', 'danger')

    return render_template(
        'rename_column.html',
        columns=columns,
        column_names=column_names,
        rename=rename,
        rename_to=rename_to,
        table=table)

@app.route('/<table>/add-index/', methods=['GET', 'POST'])
@require_table
def add_index(table):
    dataset = get_dataset()
    request_data = get_request_data()
    indexed_columns = request_data.getlist('indexed_columns')
    unique = bool(request_data.get('unique'))

    columns = dataset.get_columns(table)

    if request.method == 'POST':
        if indexed_columns:
            try:
                migrate(
                    dataset._migrator.add_index(
                        table,
                        indexed_columns,
                        unique))
            except Exception as exc:
                flash('Error attempting to create index: %s' % exc, 'danger')
                app.logger.exception('Error attempting to create index.')
            else:
                flash('Index created successfully.', 'success')
                return redirect(url_for('table_structure', table=table))
        else:
            flash('One or more columns must be selected.', 'danger')

    return render_template(
        'add_index.html',
        columns=columns,
        indexed_columns=indexed_columns,
        table=table,
        unique=unique)

@app.route('/<table>/drop-index/', methods=['GET', 'POST'])
@require_table
def drop_index(table):
    dataset = get_dataset()
    request_data = get_request_data()
    name = request_data.get('name', '')
    indexes = dataset.get_indexes(table)
    index_names = [index.name for index in indexes]

    if request.method == 'POST':
        if name in index_names:
            try:
                migrate(dataset._migrator.drop_index(table, name))
            except Exception as exc:
                flash('Error attempting to drop index: %s' % exc, 'danger')
                app.logger.exception('Error attempting to drop index.')
            else:
                flash('Index "%s" was dropped successfully!' % name, 'success')
                return redirect(url_for('table_structure', table=table))
        else:
            flash('Index name is required.', 'danger')

    return render_template(
        'drop_index.html',
        indexes=indexes,
        index_names=index_names,
        name=name,
        table=table)

@app.route('/<table>/drop-trigger/', methods=['GET', 'POST'])
@require_table
def drop_trigger(table):
    dataset = get_dataset()
    request_data = get_request_data()
    name = request_data.get('name', '')
    triggers = dataset.get_triggers(table)
    trigger_names = [trigger.name for trigger in triggers]

    if request.method == 'POST':
        if name in trigger_names:
            try:
                dataset.query('DROP TRIGGER "%s";' % name)
            except Exception as exc:
                flash('Error attempting to drop trigger: %s' % exc, 'danger')
                app.logger.exception('Error attempting to drop trigger.')
            else:
                flash('Trigger "%s" was dropped successfully!' % name, 'success')
                return redirect(url_for('table_structure', table=table))
        else:
            flash('Trigger name is required.', 'danger')

    return render_template(
        'drop_trigger.html',
        triggers=triggers,
        trigger_names=trigger_names,
        name=name,
        table=table)


@app.route('/<table>/content/', methods=['GET', 'POST'])
@require_table
def table_content(table):
    dataset = get_dataset()
    dataset.update_cache(table)
    ds_table = dataset[table]
    model = ds_table.model_class
    is_composite_pk = isinstance(model._meta.primary_key, CompositeKey)

    allow_edit = not dataset.is_readonly and model._meta.primary_key is not False
    allow_bulk = allow_edit and not is_composite_pk

    if request.method == 'POST':
        if not allow_bulk:
            flash('Cannot perform bulk operation on this table.', 'warning')
        else:
            action = request.form['action']
            pks = request.form.getlist('pk')
            if not pks:
                flash('No rows were selected.', 'warning')
            elif action == 'bulk-delete':
                n = (model.delete()
                     .where(model._meta.primary_key.in_(pks))
                     .execute())
                flash('Successfully deleted %s row(s)' % n, 'success')
            else:
                flash('Unrecognized action', 'warning')
        return redirect(request.full_path)

    page_number = request.args.get('page') or ''
    if page_number == 'last': page_number = '1000000'
    page_number = int(page_number) if page_number.isdigit() else 1

    total_rows = ds_table.all().count()
    rows_per_page = app.config['ROWS_PER_PAGE']
    total_pages = max(1, int(math.ceil(total_rows / float(rows_per_page))))
    # Restrict bounds.
    page_number = max(min(page_number, total_pages), 1)

    previous_page = page_number - 1 if page_number > 1 else None
    next_page = page_number + 1 if page_number < total_pages else None

    ordering = request.args.get('ordering')
    fk_labels = get_foreign_key_labels_for_table(table, FOREIGN_KEY_LABELS)
    label_columns = []

    if fk_labels:
        offset = (page_number - 1) * rows_per_page
        content_sql = build_labeled_content_sql(
            table,
            fk_labels,
            ordering=ordering,
            limit=rows_per_page,
            offset=offset)
        columns, query = fetch_labeled_content_rows(dataset, content_sql)
        field_names = columns
        label_columns = [label_column_alias(label.local_column)
                         for label in fk_labels]
    else:
        query = ds_table.all().paginate(page_number, rows_per_page)
        if ordering:
            field = model._meta.columns[ordering.lstrip('-')]
            if ordering.startswith('-'):
                field = field.desc()
            query = query.order_by(field)
        field_names = ds_table.columns
        columns = [f.column_name for f in ds_table.model_class._meta.sorted_fields]

    session['%s.last_viewed' % table] = (page_number, ordering)

    return render_template(
        'table_content.html',
        allow_bulk=allow_bulk,
        allow_edit=allow_edit,
        columns=columns,
        ds_table=ds_table,
        field_names=field_names,
        is_composite_pk=isinstance(model._meta.primary_key, CompositeKey),
        label_columns=label_columns,
        next_page=next_page,
        ordering=ordering,
        page=page_number,
        previous_page=previous_page,
        query=query,
        table=table,
        table_pk=model._meta.primary_key,
        table_sql=dataset.get_table_sql(table),
        total_pages=total_pages,
        total_rows=total_rows)

def minimal_validate_field(field, value):
    if value.lower().strip() == 'null':
        value = None
    if value is None and not field.null:
        return 'NULL', 'Column does not allow NULL values.'
    if value is None:
        return None, None
    if isinstance(field, IntegerField):
        try:
            _ = int(value)
        except Exception:
            return value, 'Value is not a number.'
    elif isinstance(field, FloatField):
        try:
            _ = float(value)
        except Exception:
            return value, 'Value is not a numeric/real.'
    elif isinstance(field, BooleanField):
        if value.lower() not in ('1', '0', 'true', 'false', 't', 'f'):
            return value, 'Value must be 1, 0, true, false, t or f.'
        value = True if value.lower() in ('1', 't', 'true') else False
    elif isinstance(field, BlobField):
        if app.config['BLOB_AS_BASE64']:
            try:
                value = base64.b64decode(value)
            except Exception as exc:
                return value, 'Value must be base64-encoded binary data.'
        else:
            try:
                value = bytes.fromhex(value)
            except Exception as exc:
                return value, 'Value must be valid hex representation.'
    try:
        field.db_value(value)
    except Exception as exc:
        return value, str(exc)

    return value, None

@app.route('/<table>/insert/', methods=['GET', 'POST'])
@require_table
def table_insert(table):
    dataset = get_dataset()
    dataset.update_cache(table)
    model = dataset[table].model_class

    columns = []
    fields = []
    defaults = {}
    row = {}
    for column in dataset.get_columns(table):
        field = model._meta.columns[column.name]
        if isinstance(field, AutoField):
            continue
        if column.default:
            defaults[column.name] = column.default
        columns.append(column)
        fields.append(field)
        row[field.name] = ''

    edited = set()
    errors = {}
    if request.method == 'POST':
        insert = {}
        for key, value in request.form.items():
            if key not in model._meta.fields: continue
            field = model._meta.fields[key]
            edited.add(field.name)
            row[field.name] = value

            value, err = minimal_validate_field(field, value)
            if err:
                errors[key] = err
            else:
                insert[field] = value

        if errors:
            flash('One or more errors prevented the row being inserted.',
                  'danger')
        elif insert:
            try:
                with dataset.transaction() as txn:
                    n = model.insert(insert).execute()
            except Exception as exc:
                flash('Insert failed: %s' % exc, 'danger')
                app.logger.exception('Error attempting to insert row into %s.', table)
            else:
                flash('Successfully inserted record (%s).' % n, 'success')
                return redirect(url_for(
                    'table_content',
                    table=table,
                    page='last'))
        else:
            flash('No data was specified to be inserted.', 'warning')
    else:
        edited = set(model._meta.sorted_field_names) - set(defaults)  # Make all fields editable on load.

    columns_fields = zip(columns, fields)

    return render_template(
        'table_insert.html',
        columns_fields=columns_fields,
        defaults=defaults,
        edited=edited,
        errors=errors,
        model=model,
        row=row,
        table=table)

def redirect_to_previous(table):
    page_ordering = session.get('%s.last_viewed' % table)
    if not page_ordering:
        return redirect(url_for('table_content', table=table))
    page, ordering = page_ordering
    kw = {}
    if page and page != 1:
        kw['page'] = page
    if ordering:
        kw['ordering'] = ordering
    return redirect(url_for('table_content', table=table, **kw))

@app.route('/<table>/update/<b64:pk>/', methods=['GET', 'POST'])
@require_table
def table_update(table, pk):
    dataset = get_dataset()
    dataset.update_cache(table)
    model = dataset[table].model_class
    table_pk = model._meta.primary_key
    if not table_pk:
        flash('Table must have a primary key to perform update.', 'danger')
        return redirect(url_for('table_content', table=table))
    elif pk == '__uneditable__':
        flash('Could not encode primary key to perform update.', 'danger')
        return redirect(url_for('table_content', table=table))

    expr = decode_pk(model, pk)
    try:
        obj = model.get(expr)
    except model.DoesNotExist:
        pk_repr = pk_display(table_pk, pk)
        flash('Could not fetch row with primary-key %s.' % str(pk_repr), 'danger')
        return redirect(url_for('table_content', table=table))

    columns = []
    fields = []
    for column in dataset.get_columns(table):
        columns.append(column)
        fields.append(model._meta.columns[column.name])

    row = {}
    for field in fields:
        value = getattr(obj, field.name)
        if value is None:
            row[field.name] = None
        elif isinstance(field, BlobField):
            if app.config['BLOB_AS_BASE64']:
                row[field.name] = base64.b64encode(value).decode('utf8')
            else:
                row[field.name] = value.hex()
        else:
            row[field.name] = value

    edited = set()
    errors = {}
    if request.method == 'POST':
        update = {}
        for key, value in request.form.items():
            if key not in model._meta.fields: continue
            field = model._meta.fields[key]
            edited.add(field.name)
            row[field.name] = value

            value, err = minimal_validate_field(field, value)
            if err:
                errors[key] = err
            else:
                update[field] = value

        if errors:
            flash('One or more errors prevented the row being updated.',
                  'danger')
        elif update:
            try:
                with dataset.transaction() as txn:
                    n = model.update(update).where(expr).execute()
            except Exception as exc:
                flash('Update failed: %s' % exc, 'danger')
                app.logger.exception('Error attempting to update row from %s.', table)
            else:
                flash('Successfully updated %s record.' % n, 'success')
                return redirect_to_previous(table)
        else:
            flash('No data was specified to be updated.', 'warning')

    columns_fields = zip(columns, fields)

    return render_template(
        'table_update.html',
        columns_fields=columns_fields,
        edited=edited,
        errors=errors,
        fields=fields,
        model=model,
        pk=pk,
        row=row,
        table=table,
        table_pk=model._meta.primary_key)

@app.route('/<table>/delete/<b64:pk>/', methods=['GET', 'POST'])
@require_table
def table_delete(table, pk):
    dataset = get_dataset()
    dataset.update_cache(table)
    model = dataset[table].model_class
    table_pk = model._meta.primary_key
    if not table_pk:
        flash('Table must have a primary key to perform delete.', 'danger')
        return redirect(url_for('table_content', table=table))
    elif pk == '__uneditable__':
        flash('Could not encode primary key to perform delete.', 'danger')
        return redirect(url_for('table_content', table=table))

    expr = decode_pk(model, pk)
    try:
        row = model.select().where(expr).dicts().get()
    except model.DoesNotExist:
        pk_repr = pk_display(table_pk, pk)
        flash('Could not fetch row with primary-key %s.' % str(pk_repr), 'danger')
        return redirect(url_for('table_content', table=table))

    if request.method == 'POST':
        try:
            with dataset.transaction() as txn:
                n = model.delete().where(expr).execute()
        except Exception as exc:
            flash('Delete failed: %s' % exc, 'danger')
            app.logger.exception('Error attempting to delete row from %s.', table)
        else:
            flash('Successfully deleted %s record.' % n, 'success')
            return redirect_to_previous(table)

    return render_template(
        'table_delete.html',
        model=model,
        pk=pk,
        row=row,
        table=table,
        table_pk=table_pk)

@app.route('/<table>/query/', methods=['GET', 'POST'])
@require_table
def table_query(table):
    return _query_view('table_query.html', table)

def export(query, export_format, table=None):
    dataset = get_dataset()
    buf = StringIO()
    if export_format == 'json':
        kwargs = {'indent': 2}
        filename = 'export.json'
        mimetype = 'application/json'
    else:
        kwargs = {}
        filename = 'export.csv'
        mimetype = 'text/csv'

    if table:
        filename = '%s-%s' % (table, filename)

    # Avoid any special chars in export filename.
    filename = re.sub(r'[^\w\d\-\.]+', '', filename)

    if peewee_version >= (4, 0, 2):
        kwargs['base64_bytes'] = app.config['BLOB_AS_BASE64']

    dataset.freeze(query, export_format, file_obj=buf, **kwargs)

    response_data = buf.getvalue().encode('utf8')
    response = make_response(response_data)
    response.headers['Content-Length'] = len(response_data)
    response.headers['Content-Type'] = mimetype
    response.headers['Content-Disposition'] = 'attachment; filename="%s"' % (
        filename)
    response.headers['Expires'] = 0
    response.headers['Pragma'] = 'public'
    return response

@app.route('/<table>/export/', methods=['GET', 'POST'])
@require_table
def table_export(table):
    dataset = get_dataset()
    columns = dataset.get_columns(table)
    if request.method == 'POST':
        export_format = request.form.get('export_format') or 'json'
        col_dict = {c.name: c for c in columns}
        selected = [c for c in (request.form.getlist('columns') or [])
                    if c in col_dict]
        if not selected:
            flash('Please select one or more columns to export.', 'danger')
        else:
            model = dataset[table].model_class
            fields = [model._meta.columns[c] for c in selected]
            query = model.select(*fields).dicts()
            try:
                return export(query, export_format, table)
            except Exception as exc:
                flash('Error generating export: %s' % exc, 'danger')
                app.logger.exception('Error generating export.')

    return render_template(
        'table_export.html',
        columns=columns,
        table=table)

@app.route('/<table>/import/', methods=['GET', 'POST'])
@require_table
def table_import(table):
    dataset = get_dataset()
    count = None
    request_data = get_request_data()
    strict = bool(request_data.get('strict'))

    if request.method == 'POST':
        file_obj = request.files.get('file')
        if not file_obj:
            flash('Please select an import file.', 'danger')
        elif not file_obj.filename.lower().endswith(('.csv', '.json')):
            flash('Unsupported file-type. Must be a .json or .csv file.',
                  'danger')
        else:
            if file_obj.filename.lower().endswith('.json'):
                format = 'json'
            else:
                format = 'csv'

            # Here we need to translate the file stream. Werkzeug uses a
            # spooled temporary file opened in wb+ mode, which is not
            # compatible with Python's CSV module. We'd need to reach pretty
            # far into Flask's internals to modify this behavior, so instead
            # we'll just translate the stream into utf8-decoded unicode.
            try:
                stream = TextIOWrapper(file_obj, encoding='utf8')
            except AttributeError:
                # The SpooledTemporaryFile used by werkzeug does not
                # implement an API that the TextIOWrapper expects, so we'll
                # just consume the whole damn thing and decode it.
                # Fixed in werkzeug 0.15.
                stream = StringIO(file_obj.read().decode('utf8'))

            kwargs = {}
            if peewee_version >= (4, 0, 2):
                kwargs['base64_bytes'] = app.config['BLOB_AS_BASE64']
            try:
                with dataset.transaction():
                    count = dataset.thaw(
                        table,
                        format=format,
                        file_obj=stream,
                        strict=strict,
                        **kwargs)
            except Exception as exc:
                flash('Error importing file: %s' % exc, 'danger')
                app.logger.exception('Error importing file.')
            else:
                flash(
                    'Successfully imported %s objects from %s.' % (
                        count, file_obj.filename),
                    'success')
                return redirect(url_for('table_content', table=table))

    return render_template(
        'table_import.html',
        count=count,
        strict=strict,
        table=table)

@app.route('/<table>/drop/', methods=['GET', 'POST'])
@require_table
def drop_table(table):
    dataset = get_dataset()
    is_view = any(v.name == table for v in dataset.get_all_views())
    label = 'view' if is_view else 'table'
    if request.method == 'POST':
        try:
            if is_view:
                dataset.query('DROP VIEW "%s";' % table)
            else:
                model_class = dataset[table].model_class
                model_class.drop_table()
        except Exception as exc:
            flash('Error attempting to drop %s "%s".' % (label, table), 'danger')
            app.logger.exception('Error attempting to drop %s "%s".', label, table)
        else:
            dataset.update_cache()  # Update all tables.
            flash('%s "%s" dropped successfully.' %
                  ('view' if is_view else 'table', table),
                  'success')
            return redirect(url_for('index'))

    return render_template('drop_table.html', is_view=is_view, table=table)

@app.template_filter('format_index')
def format_index(index_sql):
    split_regex = re.compile(r'\bon\b', re.I)
    if not split_regex.search(index_sql):
        return index_sql

    create, definition = split_regex.split(index_sql)
    return '\nON '.join((create.strip(), definition.strip()))

@app.template_filter('encode_pk')
def encode_pk(row, pk):
    if isinstance(pk, CompositeKey):
        try:
            return ':::'.join([str(row[k]) for k in pk.field_names])
        except Exception as exc:
            return '__uneditable__'
    return row[pk.column_name]

def decode_pk(model, pk_data):
    pk = model._meta.primary_key
    if isinstance(pk, CompositeKey):
        fields = [pk.model._meta.columns[f] for f in pk.field_names]
        values = pk_data.split(':::')
        expressions = [(f == v) for f, v in zip(fields, values)]
        return reduce(operator.and_, expressions)
    return (pk == pk_data)

@app.template_filter('pk_display')
def pk_display(table_pk, pk):
    if isinstance(table_pk, CompositeKey):
        return tuple(pk.split(':::'))
    elif isinstance(pk, bytes):
        pk = base64.b64encode(pk).decode()
    return pk

link_re = re.compile(r'(?:https?|mailto)://[^\s]+')

@app.template_filter('value_filter')
def value_filter(value, max_length=50):
    if isinstance(value, (int, float)):
        return value

    if isinstance(value, (bytes, bytearray, memoryview)):
        try:
            value = value.decode('utf8')
        except UnicodeDecodeError:
            if app.config['BLOB_AS_BASE64']:
                value = base64.b64encode(value)[:1024].decode('utf8')
            else:
                value = value.hex()[:1024]
    if isinstance(value, str):
        if link_re.match(value):
            label = value
            if len(value) > max_length and app.config['TRUNCATE_VALUES']:
                label = value[:max_length]
            return '<a href="%s">%s</a>' % (escape(value), escape(label))
        value = escape(value)
        if len(value) > max_length and app.config['TRUNCATE_VALUES']:
            return ('<span class="truncated">%s</span> '
                    '<span class="full" style="display:none;">%s</span>'
                    '<a class="toggle-value" href="#">...</a>') % (
                        value[:max_length],
                        value)
    return value

column_re = re.compile(r'(.+?)\((.+)\)', re.S)
column_split_re = re.compile(r'(?:[^,(]|\([^)]*\))+')

def _format_create_table(sql):
    create_table, column_list = column_re.search(sql).groups()
    columns = ['  %s' % column.strip()
               for column in column_split_re.findall(column_list)
               if column.strip()]
    return '%s (\n%s\n)' % (
        create_table,
        ',\n'.join(columns))

@app.template_filter()
def format_create_table(sql):
    try:
        return _format_create_table(sql)
    except:
        return sql

@app.template_filter('highlight')
def highlight_filter(data):
    return Markup(syntax_highlight(data))

def get_query_images():
    accum = []
    image_dir = os.path.join(app.static_folder, 'img')
    if not os.path.exists(image_dir):
        return accum
    for filename in sorted(os.listdir(image_dir)):
        basename = os.path.splitext(os.path.basename(filename))[0]
        parts = basename.split('-')
        accum.append((parts, 'img/' + filename))
    return accum

#
# Flask application helpers.
#

@app.context_processor
def _general():
    dataset = get_dataset()
    return {
        'dataset': dataset,
        'datasets': datasets,
        'enable_load': app.config.get('ENABLE_LOAD'),
        'enable_filesystem': app.config.get('ENABLE_FILESYSTEM'),
        'login_required': bool(app.config.get('PASSWORD')),
        'saved_queries': get_all_saved_queries(dataset),
        'version': __version__,
    }

@app.context_processor
def _now():
    return {'now': datetime.datetime.now()}

@app.before_request
def _connect_db():
    dataset = get_dataset()
    dataset._database.connect(reuse_if_open=True)
    if dataset_config.get('startup_hook'):
        dataset_config['startup_hook'](dataset._database)

@app.teardown_request
def _close_db(exc):
    dataset = get_dataset()
    if not dataset._database.is_closed():
        dataset.close()


class PrefixMiddleware(object):
    def __init__(self, app, prefix):
        self.app = app
        self.prefix = '/%s' % prefix.strip('/')
        self.prefix_len = len(self.prefix)

    def __call__(self, environ, start_response):
        if environ['PATH_INFO'].startswith(self.prefix):
            environ['PATH_INFO'] = environ['PATH_INFO'][self.prefix_len:]
            environ['SCRIPT_NAME'] = self.prefix
            return self.app(environ, start_response)
        else:
            start_response('404', [('Content-Type', 'text/plain')])
            return ['URL does not match application prefix.'.encode()]

#
# Script options.
#

def get_option_parser():
    parser = optparse.OptionParser()
    parser.add_option(
        '-p',
        '--port',
        default=8080,
        help='Port for web interface, default=8080',
        type='int')
    parser.add_option(
        '-H',
        '--host',
        default='127.0.0.1',
        help='Host for web interface, default=127.0.0.1')
    parser.add_option(
        '-d',
        '--debug',
        action='store_true',
        help='Run server in debug mode')
    parser.add_option(
        '-x',
        '--no-browser',
        action='store_false',
        default=False,
        dest='browser',
        help='Do not automatically open browser page.')
    parser.add_option(
        '-b',
        '--browser',
        action='store_true',
        default=False,
        dest='browser',
        help='Automatically open browser page.')
    parser.add_option(
        '-l',
        '--log-file',
        dest='log_file',
        help='Filename for application logs.')
    parser.add_option(
        '-q',
        '--quiet',
        action='store_true',
        dest='quiet',
        help='Only log errors to console.')
    parser.add_option(
        '-P',
        '--password',
        action='store_true',
        dest='prompt_password',
        help='Prompt for password to access database browser.')
    parser.add_option(
        '-r',
        '--read-only',
        action='store_true',
        dest='read_only',
        help='Open database in read-only mode.')
    parser.add_option(
        '-R',
        '--rows-per-page',
        default=50,
        dest='rows_per_page',
        help='Number of rows to display per page in content tab (default=50)',
        type='int')
    parser.add_option(
        '-Q',
        '--query-rows-per-page',
        default=1000,
        dest='query_rows_per_page',
        help='Number of rows to display per page in query tab (default=1000)',
        type='int')
    parser.add_option(
        '-T',
        '--no-truncate',
        action='store_false',
        default=True,
        dest='truncate_values',
        help=('Disable truncating long text values. By default text values '
              'are ellipsized after 50 characters and the full text is shown '
              'on click.'))
    parser.add_option(
        '-B',
        '--base64',
        action='store_true',
        dest='base64',
        help='BLOB data as base64 (default is hex)')
    parser.add_option(
        '-u',
        '--url-prefix',
        dest='url_prefix',
        help='URL prefix for application.')
    parser.add_option(
        '-f',
        '--foreign-keys',
        action='store_true',
        dest='foreign_keys',
        help='Enable the foreign_keys pragma.')
    parser.add_option(
        '-e',
        '--extension',
        action='append',
        dest='extensions',
        help='Path or name of loadable extension.')
    parser.add_option(
        '-s',
        '--startup-hook',
        dest='startup_hook',
        help=('Path to a startup hook used to initialize the connection '
              'before each request, e.g. my.module.some_callable'))
    parser.add_option(
        '-L',
        '--enable-load',
        action='store_true',
        dest='enable_load',
        help=('Enable loading additional databases at runtime (upload only). '
              'For adding local databases use --enable-filesystem'))
    parser.add_option(
        '-U',
        '--upload-dir',
        dest='upload_dir',
        help=('Destination directory for uploaded databases (-L). If not '
              'specified, a system tempdir will be used.'))
    parser.add_option(
        '-F',
        '--enable-filesystem',
        action='store_true',
        dest='enable_filesystem',
        help=('Enable loading additional databases by specifying on-disk '
              'path at runtime.'))
    ssl_opts = optparse.OptionGroup(parser, 'SSL options')
    ssl_opts.add_option(
        '-c',
        '--ssl-cert',
        dest='ssl_cert',
        help='SSL certificate file path.')
    ssl_opts.add_option(
        '-k',
        '--ssl-key',
        dest='ssl_key',
        help='SSL private key file path.')
    ssl_opts.add_option(
        '-a',
        '--ad-hoc',
        action='store_true',
        dest='ssl_ad_hoc',
        help='Use ad-hoc SSL context.')
    parser.add_option_group(ssl_opts)
    return parser

def die(msg, exit_code=1):
    sys.stderr.write('%s\n' % msg)
    sys.stderr.flush()
    sys.exit(exit_code)

def open_browser_tab(host, port):
    url = 'http://%s:%s/' % (host, port)

    def _open_tab(url):
        time.sleep(1.5)
        webbrowser.open_new_tab(url)

    thread = threading.Thread(target=_open_tab, args=(url,))
    thread.daemon = True
    thread.start()

def install_auth_handler(password):
    app.config['PASSWORD'] = password

    @app.before_request
    def check_password():
        if not session.get('authorized') and request.path != '/login/' and \
           not request.path.startswith(('/static/', '/favicon')):
            flash('You must log-in to view the database browser.', 'danger')
            session['next_url'] = request.base_url
            return redirect(url_for('login'))

def initialize_dataset(filename):
    dataset_kw = {}
    if peewee_version >= (3, 14, 9):
        dataset_kw['include_views'] = True

    if dataset_config['read_only']:
        if peewee_version < (3, 5, 1):
            die('Peewee 3.5.1 or newer is required for read-only access.')
        db = SqliteDatabase('file:%s?mode=ro' % filename, uri=True)
        try:
            db.connect()
        except OperationalError:
            die('Unable to open database file in read-only mode. Ensure that '
                'the database exists in order to use read-only mode.')
        db.close()
    else:
        db = SqliteDatabase(filename)

    if dataset_config['foreign_keys']:
        db.pragma('foreign_keys', True, permanent=True)

    if dataset_config['extensions']:
        # Load extensions before performing introspection.
        for ext in dataset_config['extensions']:
            db.load_extension(ext)

    if dataset_config['startup_hook']:
        dataset_config['startup_hook'](db)

    dataset = SqliteDataSet(db, bare_fields=True, **dataset_kw)
    dataset.close()
    return dataset

def initialize_app(filenames, read_only=False, password=None, url_prefix=None,
                   extensions=None, foreign_keys=None, startup_hook=None,
                   saved_queries=None, foreign_key_labels=None):
    global datasets
    global dataset_config
    global SAVED_QUERIES
    global FOREIGN_KEY_LABELS

    try:
        SAVED_QUERIES, FOREIGN_KEY_LABELS = validate_query_config(
            saved_queries=saved_queries,
            foreign_key_labels=foreign_key_labels)
    except ConfigError as exc:
        die('Invalid query configuration: %s' % exc)

    dataset_config.update(
        read_only=read_only,
        extensions=extensions,
        foreign_keys=foreign_keys,
        startup_hook=startup_hook)

    if password:
        install_auth_handler(password)

    if url_prefix:
        app.wsgi_app = PrefixMiddleware(app.wsgi_app, prefix=url_prefix)

    for filename in filenames:
        datasets[os.path.basename(filename)] = initialize_dataset(filename)

    for dataset in datasets.values():
        was_closed = dataset._database.is_closed()
        if was_closed:
            dataset.connect(reuse_if_open=True)
        try:
            for warning in validate_foreign_key_label_indexes(
                    dataset, FOREIGN_KEY_LABELS):
                app.logger.warning(warning)
        finally:
            if was_closed and not dataset._database.is_closed():
                dataset.close()

def configure_app():
    # This function exists to act as a console script entry-point.
    parser = get_option_parser()
    options, args = parser.parse_args()

    if not args:
        die('Error: missing required path to database file.')

    werkzeug_logger = logging.getLogger('werkzeug')
    if options.log_file:
        fmt = logging.Formatter('[%(asctime)s] - [%(levelname)s] - %(message)s')
        handler = WatchedFileHandler(options.log_file)
        handler.setLevel(logging.DEBUG if options.debug else logging.WARNING)
        handler.setFormatter(fmt)
        app.logger.addHandler(handler)

        # Remove default handler.
        from flask.logging import default_handler
        app.logger.removeHandler(default_handler)

    if options.quiet:
        app.logger.setLevel(logging.ERROR)
        werkzeug_logger.setLevel(logging.ERROR)

    password = None
    if options.prompt_password:
        if os.environ.get('SQLITE_WEB_PASSWORD'):
            password = os.environ['SQLITE_WEB_PASSWORD']
        else:
            while True:
                password = getpass('Enter password: ')
                password_confirm = getpass('Confirm password: ')
                if password != password_confirm:
                    print('Passwords did not match!')
                else:
                    break

    if options.rows_per_page:
        app.config['ROWS_PER_PAGE'] = options.rows_per_page
    if options.query_rows_per_page:
        app.config['QUERY_ROWS_PER_PAGE'] = options.query_rows_per_page
    if options.base64:
        app.config['BLOB_AS_BASE64'] = options.base64

    app.config['TRUNCATE_VALUES'] = options.truncate_values

    # Store reference to these config options.
    app.config['ENABLE_LOAD'] = options.enable_load
    app.config['ENABLE_FILESYSTEM'] = options.enable_filesystem
    app.config['DB_UPLOAD_DIR'] = options.upload_dir

    if options.startup_hook:
        try:
            module_path, hook_name = options.startup_hook.rsplit('.', 1)
        except Exception:
            die('startup hook must be dotted-path to module.hook_function')
        module = importlib.import_module(module_path)
        try:
            hook = getattr(module, hook_name)
        except AttributeError:
            die('Hook named "%s" not found in %s' % (hook_name, module))
    else:
        hook = None

    # Initialize the dataset instances and (optionally) authentication handler.
    initialize_app(args, options.read_only, password, options.url_prefix,
                   options.extensions, options.foreign_keys, hook)

    if options.browser:
        open_browser_tab(options.host, options.port)

    if password:
        key = b'sqlite-web-' + args[0].encode('utf8') + password.encode('utf8')
        app.secret_key = hashlib.sha256(key).hexdigest()

    # Set up SSL context, if specified.
    kwargs = {
        'host': options.host,
        'port': options.port,
        'debug': options.debug,
    }

    if options.ssl_ad_hoc:
        kwargs['ssl_context'] = 'adhoc'

    if options.ssl_cert and options.ssl_key:
        if not os.path.exists(options.ssl_cert) or not os.path.exists(options.ssl_key):
            die('ssl cert or ssl key not found. Please check the file-paths.')
        kwargs['ssl_context'] = (options.ssl_cert, options.ssl_key)
    elif options.ssl_cert:
        die('ssl key "-k" is required alongside the ssl cert')
    elif options.ssl_key:
        die('ssl cert "-c" is required alongside the ssl key')

    return kwargs


def main():
    kwargs = configure_app()  # Read options from command-line, configure app.

    # Run WSGI application.
    app.run(**kwargs)


if __name__ == '__main__':
    main()
