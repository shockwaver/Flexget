import logging
import time
import os
import base64
import re
from flexget.feed import Entry
from flexget.utils.tools import replace_from_entry, make_valid_path
from flexget.plugin import register_plugin, PluginError, priority, get_plugin_by_name, DependencyError

log = logging.getLogger('deluge')

try:
    from twisted.python import log as twisted_log
    from twisted.internet.main import installReactor
    from twisted.internet.selectreactor import SelectReactor

    class PausingReactor(SelectReactor):
        """A SelectReactor that can be paused and resumed."""

        def __init__(self):
            SelectReactor.__init__(self)
            self.paused = False
            self._return_value = None
            self._release_requested = False
            self._mainLoopGen = None

        def _mainLoopGenerator(self):
            """Generator that acts as mainLoop, but yields when requested."""
            while self._started:
                try:
                    while self._started:
                        self.iterate()

                        if self._release_requested:
                            self._release_requested = False
                            self.paused = True
                            yield self._return_value
                            self.paused = False
                except GeneratorExit:
                    raise
                except:
                    twisted_log.msg("Unexpected error in main loop.")
                    twisted_log.err()
                else:
                    twisted_log.msg('Main loop terminated.')

        def run(self, installSignalHandlers=False):
            """Starts or resumes the reactor."""
            if not self._started:
                self.startRunning(installSignalHandlers)
                self._mainLoopGen = self._mainLoopGenerator()
            try:
                return self._mainLoopGen.next()
            except StopIteration:
                pass

        def pause(self, return_value=None):
            """Causes reactor to pause after this iteration.
            If :return_value: is specified, it will be returned by the reactor.run call."""
            self._return_value = return_value
            self._release_requested = True

        def stop(self):
            """Stops the reactor."""
            SelectReactor.stop(self)
            # If this was called while the reactor was paused we have to resume in order for it to complete
            if self.paused:
                self.run()

    # Configure twisted to use the PausingReactor.
    installReactor(PausingReactor())

except ImportError:
    # If twisted is not found, errors will be shown later
    pass


# Define a base class with some methods that are used for all deluge versions
class DelugePlugin(object):
    """Base class for deluge plugins, contains settings and methods for connecting to a deluge daemon."""

    def validate_connection_info(self, dict_validator):
        dict_validator.accept('text', key='host')
        dict_validator.accept('integer', key='port')
        dict_validator.accept('text', key='user')
        dict_validator.accept('text', key='pass')

    def prepare_connection_info(self, config):
        config.setdefault('host', 'localhost')
        config.setdefault('port', 58846)
        config.setdefault('user', '')
        config.setdefault('pass', '')

    def on_process_start(self, feed, config):
        """Raise a DependencyError if our dependencies aren't available"""
        # This is overridden by OutputDeluge to add deluge 1.1 support
        try:
            from deluge.ui.client import client
        except ImportError, e:
            raise DependencyError('output_deluge', 'deluge', 'Deluge module and it\'s dependencies required. ImportError: %s' % e, log)
        try:
            from twisted.internet import reactor
        except:
            raise DependencyError('output_deluge', 'twisted.internet', 'Twisted.internet package required', log)
        log.debug('Using deluge 1.2 api')

    def on_feed_abort(self, feed, config):
        pass

# Add some more methods to the base class if we are using deluge 1.2+
try:
    from twisted.internet import reactor
    from deluge.ui.client import client

    class DelugePlugin(DelugePlugin):

        def on_disconnect(self):
            """Pauses the reactor. Gets called when we disconnect from the daemon."""
            # pause the reactor, so flexget can continue
            reactor.callLater(0, reactor.pause)

        def on_connect_fail(self, result):
            """Pauses the reactor, returns PluginError. Gets called when connection to deluge daemon fails."""
            log.debug('Connect to deluge daemon failed, result: %s' % result)
            reactor.callLater(0, reactor.pause, PluginError('Could not connect to deluge daemon', log))

        def on_connect_success(self, result, feed, config):
            """Gets called when successfully connected to the daemon. Should do the work then call client.disconnect"""
            raise NotImplementedError

        def connect(self, feed, config):
            """Connects to the deluge daemon and runs on_connect_success """

            client.set_disconnect_callback(self.on_disconnect)

            d = client.connect(
                host=config['host'],
                port=config['port'],
                username=config['user'],
                password=config['pass'])

            d.addCallback(self.on_connect_success, feed, config).addErrback(self.on_connect_fail)
            result = reactor.run()
            if isinstance(result, Exception):
                raise result
            return result

        def on_process_end(self, feed, config):
            """Shut down the twisted reactor after all feeds have run."""
            if not reactor._stopped:
                log.debug('Stopping twisted reactor.')
                reactor.stop()

        def on_feed_abort(self, feed, config):
            self.on_process_end(feed, config)

except ImportError:
    pass


class InputDeluge(DelugePlugin):
    """Create entries for torrents in the deluge session."""
    #
    settings_map = {
        'name': 'title',
        'hash': 'torrent_info_hash',
        'num_peers': 'torrent_peers',
        'num_seeds': 'torrent_seeds',
        'private': 'deluge_private',
        'state': 'deluge_state',
        'eta': 'deluge_eta',
        'ratio': 'deluge_ratio',
        'move_on_completed_path': 'deluge_movedone',
        'save_path': 'deluge_path',
        'label': 'deluge_label',
        'total_size': ('content_size', lambda size: size / 1024 / 1024),
        'files': ('content_files', lambda file_dicts: [os.path.basename(f['path']) for f in file_dicts])}

    def __init__(self):
        self.entries = []

    def validator(self):
        from flexget import validator
        root = validator.factory()
        root.accept('boolean')
        advanced = root.accept('dict')
        advanced.accept('path', key='config_path')
        self.validate_connection_info(advanced)
        filter = advanced.accept('dict', key='filter')
        filter.accept('text', key='label')
        filter.accept('choice', key='state').accept_choices(
            ['active', 'downloading', 'seeding', 'queued', 'paused'], ignore_case=True)
        return root

    def prepare_config(self, config):
        if isinstance(config, bool):
            config = {}
        if 'filter' in config:
            filter = config['filter']
            if 'label' in filter:
                filter['label'] = filter['label'].lower()
            if 'state' in filter:
                filter['state'] = filter['state'].capitalize()
        self.prepare_connection_info(config)
        return config

    def on_feed_input(self, feed, config):
        """Generates and returns a list of entries from the deluge daemon."""
        # Reset the entries list
        self.entries = []
        # Call connect, entries get generated if everything is successful
        self.connect(feed, self.prepare_config(config))
        return self.entries

    def on_connect_success(self, result, feed, config):
        """Creates a list of FlexGet entries from items loaded in deluge and stores them to self.entries"""
        from deluge.ui.client import client

        def on_get_torrents_status(torrents):
            config_path = os.path.expanduser(config.get('config_path', ''))
            for hash, torrent_dict in torrents.iteritems():
                # Make sure it has a url so no plugins crash
                entry = Entry(deluge_id=hash, url='')
                if config_path:
                    torrent_path = os.path.join(config_path, 'state', hash + '.torrent')
                    if os.path.isfile(torrent_path):
                        entry['location'] = torrent_path
                        if not torrent_path.startswith('/'):
                            torrent_path = '/' + torrent_path
                        entry['url'] = 'file://' + torrent_path
                    else:
                        log.warning('Did not find torrent file at %s' % torrent_path)
                for key, value in torrent_dict.iteritems():
                    flexget_key = self.settings_map[key]
                    if isinstance(flexget_key, tuple):
                        flexget_key, format_func = flexget_key
                        value = format_func(value)
                    entry[flexget_key] = value
                self.entries.append(entry)
            client.disconnect()
        filter = config.get('filter', {})
        client.core.get_torrents_status(filter, self.settings_map.keys()).addCallback(on_get_torrents_status)


class OutputDeluge(DelugePlugin):
    """Add the torrents directly to deluge, supporting custom save paths."""

    def validator(self):
        from flexget import validator
        root = validator.factory()
        root.accept('boolean')
        deluge = root.accept('dict')
        self.validate_connection_info(deluge)
        deluge.accept('path', key='path', allow_replacement=True)
        deluge.accept('path', key='movedone', allow_replacement=True)
        deluge.accept('text', key='label')
        deluge.accept('boolean', key='queuetotop')
        deluge.accept('boolean', key='automanaged')
        deluge.accept('number', key='maxupspeed')
        deluge.accept('number', key='maxdownspeed')
        deluge.accept('integer', key='maxconnections')
        deluge.accept('integer', key='maxupslots')
        deluge.accept('number', key='ratio')
        deluge.accept('boolean', key='removeatratio')
        deluge.accept('boolean', key='addpaused')
        deluge.accept('boolean', key='compact')
        deluge.accept('text', key='content_filename')
        deluge.accept('boolean', key='main_file_only')
        deluge.accept('boolean', key='enabled')
        return root

    def prepare_config(self, config):
        if isinstance(config, bool):
            config = {'enabled': config}
        self.prepare_connection_info(config)
        config.setdefault('enabled', True)
        config.setdefault('path', '')
        config.setdefault('movedone', '')
        config.setdefault('label', '')
        return config

    def __init__(self):
        self.deluge12 = None
        self.deluge_version = None
        self.options = {'maxupspeed': 'max_upload_speed', 'maxdownspeed': 'max_download_speed', \
            'maxconnections': 'max_connections', 'maxupslots': 'max_upload_slots', \
            'automanaged': 'auto_managed', 'ratio': 'stop_ratio', 'removeatratio': 'remove_at_ratio', \
            'addpaused': 'add_paused', 'compact': 'compact_allocation'}

    @priority(120)
    def on_process_start(self, feed, config):
        """
            Register the usable set: keywords. Detect what version of deluge is loaded.
        """
        set_plugin = get_plugin_by_name('set')
        set_plugin.instance.register_keys({'path': 'text', 'movedone': 'text', \
            'queuetotop': 'boolean', 'label': 'text', 'automanaged': 'boolean', \
            'maxupspeed': 'number', 'maxdownspeed': 'number', 'maxupslots': 'integer', \
            'maxconnections': 'integer', 'ratio': 'number', 'removeatratio': 'boolean', \
            'addpaused': 'boolean', 'compact': 'boolean', 'content_filename': 'text', 'main_file_only': 'boolean'})
        if self.deluge12 is None:
            logger = log.info if feed.manager.options.test else log.debug
            try:
                log.debug('Looking for deluge 1.1 API')
                from deluge.ui.client import sclient
                log.debug('1.1 API found')
            except ImportError:
                log.debug('Looking for deluge 1.2 API')
                DelugePlugin.on_process_start(self, feed, config)
                logger('Using deluge 1.2 api')
                self.deluge12 = True
            else:
                logger('Using deluge 1.1 api')
                self.deluge12 = False

    @priority(120)
    def on_feed_download(self, feed, config):
        """
            call download plugin to generate the temp files we will load into deluge
            then verify they are valid torrents
        """
        import deluge.ui.common
        config = self.prepare_config(config)
        if not config['enabled']:
            return
        # If the download plugin is not enabled, we need to call it to get our temp .torrent files
        if not 'download' in feed.config:
            download = get_plugin_by_name('download')
            for entry in feed.accepted:
                if not entry.get('deluge_id'):
                    download.instance.get_temp_file(feed, entry, handle_magnets=True)

        # Check torrent files are valid
        for entry in feed.accepted:
            if os.path.exists(entry.get('file', '')):
                # Check if downloaded file is a valid torrent file
                try:
                    deluge.ui.common.TorrentInfo(entry['file'])
                except Exception:
                    feed.fail(entry, 'Invalid torrent file')
                    log.error('Torrent file appears invalid for: %s', entry['title'])

    @priority(135)
    def on_feed_output(self, feed, config):
        """Add torrents to deluge at exit."""
        config = self.prepare_config(config)
        # don't add when learning
        if feed.manager.options.learn:
            return
        if not config['enabled'] or not (feed.accepted or feed.manager.options.test):
            return

        add_to_deluge = self.connect if self.deluge12 else self.add_to_deluge11
        add_to_deluge(feed, config)
        # Clean up temp file if download plugin is not configured for this feed
        if not 'download' in feed.config:
            for entry in feed.accepted + feed.failed:
                if os.path.exists(entry.get('file', '')):
                    os.remove(entry['file'])
                    del(entry['file'])

    def add_to_deluge11(self, feed, config):
        """Add torrents to deluge using deluge 1.1.x api."""
        try:
            from deluge.ui.client import sclient
        except:
            raise PluginError('Deluge module required', log)

        sclient.set_core_uri()
        for entry in feed.accepted:
            try:
                before = sclient.get_session_state()
            except Exception, (errno, msg):
                raise PluginError('Could not communicate with deluge core. %s' % msg, log)
            if feed.manager.options.test:
                return
            opts = {}
            path = entry.get('path', config['path'])
            if path:
                opts['download_location'] = os.path.expanduser(path % entry)
            for fopt, dopt in self.options.iteritems():
                value = entry.get(fopt, config.get(fopt))
                if value is not None:
                    opts[dopt] = value
                    if fopt == 'ratio':
                        opts['stop_at_ratio'] = True

            # check that file is downloaded
            if not 'file' in entry:
                feed.fail(entry, 'file missing?')
                continue

            # see that temp file is present
            if not os.path.exists(entry['file']):
                tmp_path = os.path.join(feed.manager.config_base, 'temp')
                log.debug('entry: %s' % entry)
                log.debug('temp: %s' % ', '.join(os.listdir(tmp_path)))
                feed.fail(entry, 'Downloaded temp file \'%s\' doesn\'t exist!?' % entry['file'])
                continue

            sclient.add_torrent_file([entry['file']], [opts])
            log.info('%s torrent added to deluge with options %s' % (entry['title'], opts))

            movedone = entry.get('movedone', config['movedone'])
            label = entry.get('label', config['label']).lower()
            queuetotop = entry.get('queuetotop', config.get('queuetotop'))

            # Sometimes deluge takes a moment to add the torrent, wait a second.
            time.sleep(2)
            after = sclient.get_session_state()
            for item in after:
                # find torrentid of just added torrent
                if not item in before:
                    movedone = replace_from_entry(movedone, entry, 'movedone', log.error)
                    movedone = os.path.expanduser(movedone)
                    if movedone:
                        if not os.path.isdir(movedone):
                            log.debug('movedone path %s doesn\'t exist, creating' % movedone)
                            os.makedirs(movedone)
                        log.debug('%s move on complete set to %s' % (entry['title'], movedone))
                        sclient.set_torrent_move_on_completed(item, True)
                        sclient.set_torrent_move_on_completed_path(item, movedone)
                    if label:
                        if not 'label' in sclient.get_enabled_plugins():
                            sclient.enable_plugin('label')
                        if not label in sclient.label_get_labels():
                            sclient.label_add(label)
                        log.debug('%s label set to \'%s\'' % (entry['title'], label))
                        sclient.label_set_torrent(item, label)
                    if queuetotop:
                        log.debug('%s moved to top of queue' % entry['title'])
                        sclient.queue_top([item])
                    break
            else:
                log.info('%s is already loaded in deluge. Cannot change label, movedone, or queuetotop' % entry['title'])

    def on_connect_success(self, result, feed, config):
        """Gets called when successfully connected to a daemon."""
        from deluge.ui.client import client
        from twisted.internet import reactor, defer


        if not result:
            log.debug('on_connect_success returned a failed result. BUG?')

        if feed.manager.options.test:
            log.debug('Test connection to deluge daemon successful.')
            client.disconnect()
            return

        def format_label(label):
            """Makes a string compliant with deluge label naming rules"""
            return re.sub('[^\w-]+', '_', label.lower())

        def set_torrent_options(torrent_id, entry, opts):
            """Gets called when a torrent was added to the daemon."""
            dlist = []
            if not torrent_id:
                log.error('There was an error adding %s to deluge.' % entry['title'])
                # TODO: Fail entry? How can this happen still now?
                return
            log.info('%s successfully added to deluge.' % entry['title'])
            entry['deluge_id'] = torrent_id

            def create_path(result, path):
                """Creates the specified path if deluge is older than 1.3"""
                from deluge.common import VersionSplit
                # Before 1.3, deluge would not create a non-existent move directory, so we need to.
                if VersionSplit('1.3.0') > VersionSplit(self.deluge_version):
                    if client.is_localhost():
                        if not os.path.isdir(path):
                            log.debug('path %s doesn\'t exist, creating' % path)
                            os.makedirs(path)
                    else:
                        log.warning('If path does not exist on the machine running the daemon, move will fail.')

            if opts.get('path'):
                dlist.append(version_deferred.addCallback(create_path, opts['path']))
                log.debug('Moving storage for %s to %s' % (entry['title'], opts['path']))
                dlist.append(client.core.move_storage([torrent_id], opts['path']))
            if opts['movedone']:
                dlist.append(version_deferred.addCallback(create_path, opts['movedone']))
                dlist.append(client.core.set_torrent_move_completed(torrent_id, True))
                dlist.append(client.core.set_torrent_move_completed_path(torrent_id, opts['movedone']))
                log.debug('%s move on complete set to %s' % (entry['title'], opts['movedone']))
            if opts['label']:

                def apply_label(result, torrent_id, label):
                    """Gets called after labels and torrent were added to deluge."""
                    return client.label.set_torrent(torrent_id, label)

                dlist.append(label_deferred.addCallback(apply_label, torrent_id, opts['label']))
            if opts.get('queuetotop') is not None:
                if opts['queuetotop']:
                    dlist.append(client.core.queue_top([torrent_id]))
                    log.debug('%s moved to top of queue' % entry['title'])
                else:
                    dlist.append(client.core.queue_bottom([torrent_id]))
                    log.debug('%s moved to bottom of queue' % entry['title'])
            if opts.get('content_filename') or opts.get('main_file_only'):

                def on_get_torrent_status(status):
                    """Gets called with torrent status, including file info.
                    Loops through files and renames anything qualifies for content renaming."""

                    def file_exists():
                        # Checks the download path as well as the move completed path for existence of the file
                        if os.path.exists(os.path.join(status['save_path'], filename)):
                            return True
                        elif status.get('move_on_completed') and status.get('move_on_completed_path'):
                            if os.path.exists(os.path.join(status['move_on_completed_path'], filename)):
                                return True
                        else:
                            return False

                    main_file_dlist = []
                    for file in status['files']:
                        # Only rename file if it is > 90% of the content
                        if file['size'] > (status['total_size'] * 0.9):
                            if opts.get('content_filename'):
                                filename = opts['content_filename'] + os.path.splitext(file['path'])[1]
                                counter = 1
                                if client.is_localhost():
                                    while file_exists():
                                        # Try appending a (#) suffix till a unique filename is found
                                        filename = ''.join([opts['content_filename'], '(', str(counter), ')', os.path.splitext(file['path'])[1]])
                                        counter += 1
                                else:
                                    log.debug('Cannot ensure content_filename is unique when adding to a remote deluge daemon.')
                                log.debug('File %s in %s renamed to %s' % (file['path'], entry['title'], filename))
                                main_file_dlist.append(client.core.rename_files(torrent_id, [(file['index'], filename)]))
                            if opts.get('main_file_only'):
                                file_priorities = [1 if f['index'] == file['index'] else 0 for f in status['files']]
                                main_file_dlist.append(client.core.set_torrent_file_priorities(torrent_id, file_priorities))
                            return defer.DeferredList(main_file_dlist)
                    else:
                        log.warning('No files in %s are > 90%% of content size, no files renamed.' % entry['title'])

                status_keys = ['files', 'total_size', 'save_path', 'move_on_completed_path', 'move_on_completed']
                dlist.append(client.core.get_torrent_status(torrent_id, status_keys).addCallback(on_get_torrent_status))

            return defer.DeferredList(dlist)

        def on_fail(result, feed, entry):
            """Gets called when daemon reports a failure adding the torrent."""
            log.info('%s was not added to deluge! %s' % (entry['title'], result))
            feed.fail(entry, 'Could not be added to deluge')

        # dlist is a list of deferreds that must complete before we exit
        dlist = []
        # loop through entries to get a list of labels to add
        labels = set([format_label(entry['label']) for entry in feed.accepted if entry.get('label')])
        if config.get('label'):
            labels.add(format_label(config['label']))
        label_deferred = defer.succeed(True)
        if labels:
            # Make sure the label plugin is available and enabled, then add appropriate labels

            def on_get_enabled_plugins(plugins):
                """Gets called with the list of enabled deluge plugins."""

                def on_label_enabled(result):
                    """ This runs when we verify the label plugin is enabled. """

                    def on_get_labels(d_labels):
                        """Gets available labels from deluge, and adds any new labels we need."""
                        dlist = []
                        for label in labels:
                            if not label in d_labels:
                                log.debug('Adding the label %s to deluge' % label)
                                dlist.append(client.label.add(label))
                        return defer.DeferredList(dlist)

                    return client.label.get_labels().addCallback(on_get_labels)

                if 'Label' in plugins:
                    return on_label_enabled(True)
                else:
                    # Label plugin isn't enabled, so we check if it's available and enable it.

                    def on_get_available_plugins(plugins):
                        """Gets plugins available to deluge, enables Label plugin if available."""
                        if 'Label' in plugins:
                            log.debug('Enabling label plugin in deluge')
                            return client.core.enable_plugin('Label').addCallback(on_label_enabled)
                        else:
                            log.error('Label plugin is not installed in deluge')

                    return client.core.get_available_plugins().addCallback(on_get_available_plugins)

            label_deferred = client.core.get_enabled_plugins().addCallback(on_get_enabled_plugins)
            dlist.append(label_deferred)

        def on_get_daemon_info(ver):
            """Gets called with the daemon version info, stores it in self."""
            log.debug('deluge version %s' % ver)
            self.deluge_version = ver

        version_deferred = client.daemon.info().addCallback(on_get_daemon_info)
        dlist.append(version_deferred)

        def on_get_session_state(torrent_ids):
            """Gets called with a list of torrent_ids loaded in the deluge session.
            Adds new torrents and modifies the settings for ones already in the session."""
            dlist = []
            # add the torrents
            for entry in feed.accepted:

                def add_entry(entry, opts):
                    magnet, filedump = None, None
                    """Adds an entry to the deluge session"""
                    if entry.get('url', '').startswith('magnet:'):
                        magnet = entry['url']
                    else:
                        if not os.path.exists(entry['file']):
                            feed.fail(entry, 'Downloaded temp file \'%s\' doesn\'t exist!' % entry['file'])
                            del(entry['file'])
                            return
                        try:
                            f = open(entry['file'], 'rb')
                            filedump = base64.encodestring(f.read())
                        finally:
                            f.close()

                    log.debug('Adding %s to deluge.' % entry['title'])
                    if magnet:
                        return client.core.add_torrent_magnet(magnet, opts)
                    else:
                        return client.core.add_torrent_file(entry['title'], filedump, opts)

                # Generate deluge options dict for torrent add
                path = replace_from_entry(entry.get('path', config['path']), entry, 'path', log.error)
                add_opts = {}
                if path:
                    add_opts['download_location'] = make_valid_path(os.path.expanduser(path))
                for fopt, dopt in self.options.iteritems():
                    value = entry.get(fopt, config.get(fopt))
                    if value is not None:
                        add_opts[dopt] = value
                        if fopt == 'ratio':
                            add_opts['stop_at_ratio'] = True
                # Make another set of options, that get set after the torrent has been added
                content_filename = entry.get('content_filename', config.get('content_filename', ''))
                movedone = replace_from_entry(entry.get('movedone', config['movedone']), entry, 'movedone', log.error)
                modify_opts = {'movedone': make_valid_path(os.path.expanduser(movedone)),
                        'label': format_label(entry.get('label', config['label'])),
                        'queuetotop': entry.get('queuetotop', config.get('queuetotop', None)),
                        'content_filename': replace_from_entry(content_filename, entry, 'content_filename', log.error),
                        'main_file_only': entry.get('main_file_only', config.get('main_file_only', False))}

                torrent_id = entry.get('deluge_id') or entry.get('torrent_info_hash')
                torrent_id = torrent_id and torrent_id.lower()
                if torrent_id in torrent_ids:
                    log.info('%s is already loaded in deluge, setting options' % entry['title'])
                    # Entry has a deluge id, verify the torrent is still in the deluge session and apply options
                    # Since this is already loaded in deluge, we may also need to change the path
                    modify_opts['path'] = add_opts.pop('download_location', None)
                    dlist.extend([set_torrent_options(torrent_id, entry, modify_opts),
                                  client.core.set_torrent_options([torrent_id], add_opts)])
                else:
                    dlist.append(add_entry(entry, add_opts).addCallbacks(set_torrent_options, on_fail,
                            callbackArgs=(entry, modify_opts), errbackArgs=(feed, entry)))
            return defer.DeferredList(dlist)
        dlist.append(client.core.get_session_state().addCallback(on_get_session_state))

        def on_complete(result):
            """Gets called when all of our tasks for deluge daemon are complete."""
            client.disconnect()
        tasks = defer.DeferredList(dlist).addBoth(on_complete)

        def on_timeout(result):
            """Gets called if tasks have not completed in 30 seconds.
            Should only happen when something goes wrong."""
            log.error('Timed out while adding torrents to deluge.')
            log.debug('dlist: %s' % result.resultList)
            client.disconnect()

        # Schedule a disconnect to happen in 30 seconds if FlexGet hangs while connected to Deluge
        reactor.callLater(30, lambda: tasks.called or on_timeout(tasks))

    def on_feed_exit(self, feed, config):
        """Make sure all temp files are cleaned up when feed exits"""
        # If download plugin is enabled, it will handle cleanup.
        if not 'download' in feed.config:
            download = get_plugin_by_name('download')
            download.instance.cleanup_temp_files(feed)

    def on_feed_abort(self, feed, config):
        """Make sure normal cleanup tasks still happen on abort."""
        DelugePlugin.on_feed_abort(self, feed, config)
        self.on_feed_exit(feed, config)


register_plugin(InputDeluge, 'input_deluge', api_ver=2)
register_plugin(OutputDeluge, 'deluge', api_ver=2)