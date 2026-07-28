App = window.App || {};

(function(exports, $) {
    var BASE_ALIAS = 't';

    function joinAlias(index) {
        return 'r' + index;
    }

    function JoinBuilder(options) {
        this.tablesUrl = options.tablesUrl;
        this.tableUrlTemplate = options.tableUrlTemplate;
        this.suggestionsUrl = options.suggestionsUrl;
        this.buildUrl = options.buildUrl;
        this.queryUrl = options.queryUrl;
        this.defaultLimit = options.defaultLimit || 100;

        this.baseTable = '';
        this.baseMetadata = null;
        this.selectedJoins = [];
        this.availableSuggestions = [];
        this.generatedSql = '';
    }

    JoinBuilder.prototype.initialize = function() {
        this.baseSelect = $('#join-base-table');
        this.joinList = $('#join-builder-joins');
        this.columnList = $('#join-builder-columns');
        this.limitInput = $('#join-builder-limit');
        this.preview = $('#join-builder-sql');
        this.warnings = $('#join-builder-warnings');
        this.buildBtn = $('#join-builder-build');
        this.runBtn = $('#join-builder-run');
        this.copyBtn = $('#join-builder-copy');

        this.limitInput.val(this.defaultLimit);
        this.bindHandlers();
        this.loadTables();
    };

    JoinBuilder.prototype.bindHandlers = function() {
        var self = this;
        this.baseSelect.on('change', function() {
            self.onBaseTableChange($(this).val());
        });
        this.buildBtn.on('click', function(e) {
            e.preventDefault();
            self.buildSql(false);
        });
        this.runBtn.on('click', function(e) {
            e.preventDefault();
            self.buildSql(true);
        });
        this.copyBtn.on('click', function(e) {
            e.preventDefault();
            self.copySql();
        });
        this.joinList.on('change', 'input.join-toggle', function() {
            self.syncSelectedJoins();
            self.refreshSuggestions();
            self.renderColumns();
        });
        this.columnList.on('change', 'input.column-toggle', function() {
            /* selection read on build */
        });
    };

    JoinBuilder.prototype.loadTables = function() {
        var self = this;
        $.getJSON(this.tablesUrl, function(data) {
            self.baseSelect.empty();
            self.baseSelect.append(
                $('<option>', { value: '', text: 'Select base table...' }));
            $.each(data.tables || [], function(_, table) {
                self.baseSelect.append($('<option>', { value: table, text: table }));
            });
        });
    };

    JoinBuilder.prototype.onBaseTableChange = function(table) {
        this.baseTable = table;
        this.selectedJoins = [];
        this.availableSuggestions = [];
        this.generatedSql = '';
        this.preview.val('');
        this.warnings.empty();
        this.joinList.empty();
        this.columnList.empty();
        if (!table) {
            return;
        }
        var self = this;
        var url = this.tableUrlTemplate.replace('__TABLE__', encodeURIComponent(table));
        $.getJSON(url, function(data) {
            self.baseMetadata = data;
            self.refreshSuggestions();
            self.renderColumns();
        });
    };

    JoinBuilder.prototype.refreshSuggestions = function() {
        var self = this;
        if (!this.baseTable) {
            return;
        }
        $.ajax({
            url: this.suggestionsUrl,
            method: 'POST',
            contentType: 'application/json',
            data: JSON.stringify({
                base_table: this.baseTable,
                joins: this.selectedJoins
            }),
            success: function(data) {
                self.availableSuggestions = data.suggestions || [];
                self.renderJoinSuggestions();
            },
            error: function(xhr) {
                var message = 'Unable to load join suggestions.';
                if (xhr.responseJSON && xhr.responseJSON.error) {
                    message = xhr.responseJSON.error;
                }
                self.joinList.html(
                    $('<div>', { class: 'text-danger', text: message }));
            }
        });
    };

    JoinBuilder.prototype.renderJoinSuggestions = function() {
        var self = this;
        this.joinList.empty();
        if (!this.availableSuggestions.length) {
            this.joinList.append(
                $('<p>', { class: 'text-muted', text: 'No foreign-key joins found.' }));
            return;
        }
        $.each(this.availableSuggestions, function(index, suggestion) {
            var key = self.joinKey(suggestion);
            var checked = self.isJoinSelected(suggestion);
            var item = $('<div>', { class: 'form-check' });
            var input = $('<input>', {
                type: 'checkbox',
                class: 'form-check-input join-toggle',
                id: 'join-' + index,
                'data-key': key
            }).prop('checked', checked);
            input.data('join', suggestion);
            item.append(input);
            item.append($('<label>', {
                class: 'form-check-label',
                'for': 'join-' + index,
                text: suggestion.label
            }));
            self.joinList.append(item);
        });
    };

    JoinBuilder.prototype.joinKey = function(join) {
        return [
            join.local_alias || BASE_ALIAS,
            join.local_column,
            join.table,
            join.referenced_column
        ].join('|');
    };

    JoinBuilder.prototype.isJoinSelected = function(join) {
        var key = this.joinKey(join);
        for (var i = 0; i < this.selectedJoins.length; i++) {
            if (this.joinKey(this.selectedJoins[i]) === key) {
                return true;
            }
        }
        return false;
    };

    JoinBuilder.prototype.syncSelectedJoins = function() {
        var self = this;
        var joins = [];
        this.joinList.find('input.join-toggle:checked').each(function() {
            joins.push($(this).data('join'));
        });
        this.selectedJoins = joins;
    };

    JoinBuilder.prototype.aliasMap = function() {
        var aliases = {};
        aliases[BASE_ALIAS] = this.baseTable;
        for (var i = 0; i < this.selectedJoins.length; i++) {
            aliases[joinAlias(i)] = this.selectedJoins[i].table;
        }
        return aliases;
    };

    JoinBuilder.prototype.renderColumns = function() {
        var self = this;
        this.columnList.empty();
        if (!this.baseMetadata) {
            return;
        }

        var groups = [{ alias: BASE_ALIAS, table: this.baseTable, columns: this.baseMetadata.columns }];
        var loadPromises = [];

        $.each(this.selectedJoins, function(index, join) {
            var alias = joinAlias(index);
            var deferred = $.Deferred();
            loadPromises.push(deferred.promise());
            var url = self.tableUrlTemplate.replace(
                '__TABLE__', encodeURIComponent(join.table));
            $.getJSON(url, function(data) {
                groups.push({ alias: alias, table: join.table, columns: data.columns });
                deferred.resolve();
            }).fail(function() {
                deferred.resolve();
            });
        });

        $.when.apply($, loadPromises).done(function() {
            $.each(groups, function(_, group) {
                var panel = $('<div>', { class: 'mb-2' });
                panel.append($('<strong>', { text: group.alias + ' (' + group.table + ')' }));
                var list = $('<div>', { class: 'ml-3' });
                $.each(group.columns, function(_, column) {
                    var id = 'col-' + group.alias + '-' + column.name;
                    var item = $('<div>', { class: 'form-check form-check-inline' });
                    var input = $('<input>', {
                        type: 'checkbox',
                        class: 'form-check-input column-toggle',
                        id: id,
                        'data-alias': group.alias,
                        'data-column': column.name
                    });
                    if (group.alias === BASE_ALIAS) {
                        input.prop('checked', true);
                    }
                    item.append(input);
                    item.append($('<label>', {
                        class: 'form-check-label',
                        'for': id,
                        text: column.name
                    }));
                    list.append(item);
                });
                panel.append(list);
                self.columnList.append(panel);
            });
        });
    };

    JoinBuilder.prototype.collectPayload = function() {
        var columns = [];
        this.columnList.find('input.column-toggle:checked').each(function() {
            columns.push({
                table_alias: $(this).data('alias'),
                column: $(this).data('column')
            });
        });
        var limitValue = $.trim(this.limitInput.val());
        return {
            base_table: this.baseTable,
            joins: this.selectedJoins,
            columns: columns,
            limit: limitValue === '' ? null : parseInt(limitValue, 10)
        };
    };

    JoinBuilder.prototype.buildSql = function(runQuery) {
        var self = this;
        if (!this.baseTable) {
            this.warnings.html(
                $('<div>', { class: 'text-danger', text: 'Select a base table.' }));
            return;
        }
        this.syncSelectedJoins();
        $.ajax({
            url: this.buildUrl,
            method: 'POST',
            contentType: 'application/json',
            data: JSON.stringify(this.collectPayload()),
            success: function(data) {
                self.generatedSql = data.sql || '';
                self.preview.val(self.generatedSql);
                self.warnings.empty();
                $.each(data.warnings || [], function(_, warning) {
                    self.warnings.append(
                        $('<div>', { class: 'text-warning', text: warning }));
                });
                if (runQuery && self.generatedSql) {
                    self.submitQuery();
                }
            },
            error: function(xhr) {
                var message = 'Unable to build SQL.';
                if (xhr.responseJSON && xhr.responseJSON.error) {
                    message = xhr.responseJSON.error;
                }
                self.warnings.html(
                    $('<div>', { class: 'text-danger', text: message }));
            }
        });
    };

    JoinBuilder.prototype.submitQuery = function() {
        var form = $('<form>', { method: 'post', action: this.queryUrl });
        form.append($('<input>', { type: 'hidden', name: 'sql', value: this.generatedSql }));
        $('body').append(form);
        form.submit();
    };

    JoinBuilder.prototype.copySql = function() {
        var self = this;
        if (!this.generatedSql) {
            return;
        }
        if (navigator.clipboard && navigator.clipboard.writeText) {
            navigator.clipboard.writeText(this.generatedSql);
            return;
        }
        this.preview.focus();
        this.preview.select();
        document.execCommand('copy');
    };

    exports.JoinBuilder = JoinBuilder;
})(App, jQuery);
