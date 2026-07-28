App = window.App || {};

(function(exports, $) {
    function DashboardSavedQueries(options) {
        this.listUrl = options.listUrl;
        this.saveUrl = options.saveUrl;
        this.sqlTextarea = $(options.sqlSelector);
        this.select = $(options.selectSelector);
        this.descriptions = $(options.descriptionsSelector);
        this.modal = $(options.modalSelector);
        this.nameInput = $(options.nameInputSelector);
        this.saveBtn = $(options.saveBtnSelector);
        this.addBtn = $(options.addBtnSelector);
        this.errorBox = $(options.errorSelector);
    }

    DashboardSavedQueries.prototype.initialize = function() {
        var self = this;
        this.addBtn.on('click', function(e) {
            e.preventDefault();
            if (!$.trim(self.sqlTextarea.val())) {
                self.showError('Enter SQL before saving.');
                return;
            }
            self.errorBox.empty();
            self.modal.find('.modal-body .text-danger').remove();
            self.nameInput.val('');
            self.modal.modal({ keyboard: true });
        });

        this.saveBtn.on('click', function(e) {
            e.preventDefault();
            self.saveQuery();
        });

        this.modal.find('form').on('submit', function(e) {
            e.preventDefault();
            self.saveQuery();
        });

        this.refreshList();
    };

    DashboardSavedQueries.prototype.showError = function(message) {
        this.errorBox.html($('<div>', { class: 'text-danger', text: message }));
    };

    DashboardSavedQueries.prototype.saveQuery = function() {
        var self = this;
        var name = $.trim(this.nameInput.val());
        var sql = $.trim(this.sqlTextarea.val());
        if (!name) {
            self.modal.find('.modal-body .text-danger').remove();
            self.modal.find('.modal-body').append(
                $('<div>', { class: 'text-danger', text: 'Query name is required.' }));
            return;
        }
        if (!sql) {
            self.modal.find('.modal-body .text-danger').remove();
            self.modal.find('.modal-body').append(
                $('<div>', { class: 'text-danger', text: 'SQL is required.' }));
            return;
        }

        $.ajax({
            url: this.saveUrl,
            method: 'POST',
            contentType: 'application/json',
            data: JSON.stringify({ name: name, sql: sql }),
            success: function(data) {
                self.modal.modal('hide');
                self.errorBox.empty();
                self.refreshList(data.query);
            },
            error: function(xhr) {
                var message = 'Unable to save query.';
                if (xhr.responseJSON && xhr.responseJSON.error) {
                    message = xhr.responseJSON.error;
                }
                self.modal.find('.modal-body .text-danger').remove();
                self.modal.find('.modal-body').append(
                    $('<div>', { class: 'text-danger', text: message }));
            }
        });
    };

    DashboardSavedQueries.prototype.refreshList = function(selectQuery) {
        var self = this;
        $.getJSON(this.listUrl, function(data) {
            self.renderQueries(data.queries || [], selectQuery);
        });
    };

    DashboardSavedQueries.prototype.renderQueries = function(queries, selectQuery) {
        var self = this;
        this.select.empty();
        this.select.append($('<option>', {
            value: '',
            text: 'Select a saved query...'
        }));

        if (this.descriptions.length) {
            this.descriptions.empty();
        }

        $.each(queries, function(_, query) {
            var label = query.title;
            if (query.source === 'user') {
                label += ' (custom)';
            }
            var option = $('<option>', {
                value: query.run_url || '',
                text: label
            });
            if (selectQuery && query.id === selectQuery.id &&
                    query.source === selectQuery.source) {
                option.prop('selected', true);
            }
            self.select.append(option);

            if (self.descriptions.length) {
                self.descriptions.append(
                    $('<span>', { class: 'd-block' }).append(
                        $('<strong>', { text: label }),
                        document.createTextNode(' — ' + (query.description || ''))
                    )
                );
            }
        });

        if (!queries.length && this.descriptions.length) {
            this.descriptions.append(
                $('<span>', {
                    class: 'd-block text-muted',
                    text: 'No saved queries yet. Use + to save one.'
                })
            );
        }
    };

    exports.DashboardSavedQueries = DashboardSavedQueries;
})(App, jQuery);
