# Copyright (C) 2004-2012 Aaron Swartz
#                         Brian Lalor
#                         Dean Jackson
#                         Erik Hetzner
#                         Joey Hess
#                         Lindsey Smith <lindsey.smith@gmail.com>
#                         Marcel Ackermann
#                         Martin 'Joey' Schulze
#                         Matej Cepl
#                         W. Trevor King <wking@tremily.us>
#
# This file is part of rss2email.
#
# rss2email is free software: you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free Software
# Foundation, either version 2 of the License, or (at your option) version 3 of
# the License.
#
# rss2email is distributed in the hope that it will be useful, but WITHOUT ANY
# WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR
# A PARTICULAR PURPOSE.  See the GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along with
# rss2email.  If not, see <http://www.gnu.org/licenses/>.

"""Define the ``Feed`` class for handling a single feed
"""

import collections as _collections
from email.utils import formataddr as _formataddr
import re as _re
import socket as _socket
import time as _time
import urllib.error as _urllib_error
import urllib.request as _urllib_request
import uuid as _uuid
import xml.sax as _sax
import xml.sax.saxutils as _saxutils

import feedparser as _feedparser
import html2text as _html2text

from . import __url__
from . import __version__
from . import LOG as _LOG
from . import config as _config
from . import email as _email
from . import error as _error
from . import util as _util


_feedparser.USER_AGENT = 'rss2email/{} +{}'.format(__version__, __url__)
_urllib_request.install_opener(_urllib_request.build_opener())
_SOCKET_ERRORS = []
for e in ['error', 'gaierror']:
    if hasattr(_socket, e):
        _SOCKET_ERRORS.append(getattr(_socket, e))
_SOCKET_ERRORS = tuple(_SOCKET_ERRORS)


class Feed (object):
    """Utility class for feed manipulation and storage.

    >>> import pickle
    >>> import sys
    >>> from .config import CONFIG

    >>> feed = Feed(
    ...    name='test-feed', url='http://example.com/feed.atom', to='a@b.com')
    >>> print(feed)
    test-feed (http://example.com/feed.atom -> a@b.com)
    >>> feed.section
    'feed.test-feed'
    >>> feed.from_email
    'user@rss2email.invalid'

    >>> feed.from_email = 'a@b.com'
    >>> feed.save_to_config()
    >>> feed.config.write(sys.stdout)  # doctest: +REPORT_UDIFF, +ELLIPSIS
    [DEFAULT]
    from = user@rss2email.invalid
    ...
    verbose = warning
    <BLANKLINE>
    [feed.test-feed]
    url = http://example.com/feed.atom
    from = a@b.com
    to = a@b.com
    <BLANKLINE>

    >>> feed.etag = 'dummy etag'
    >>> string = pickle.dumps(feed)
    >>> feed = pickle.loads(string)
    >>> feed.load_from_config(config=CONFIG)
    >>> feed.etag
    'dummy etag'
    >>> feed.url
    'http://example.com/feed.atom'

    Names can only contain ASCII letters, digits, and '._-'.  Here the
    invalid space causes an exception:

    >>> Feed(name='invalid name')
    Traceback (most recent call last):
      ...
    rss2email.error.InvalidFeedName: invalid feed name 'invalid name'

    Cleanup `CONFIG`.

    >>> CONFIG['DEFAULT']['to'] = ''
    >>> test_section = CONFIG.pop('feed.test-feed')
    """
    _name_regexp = _re.compile('^[a-zA-Z0-9._-]+$')

    # saved/loaded from feed.dat using __getstate__/__setstate__.
    _dynamic_attributes = [
        'name',
        'etag',
        'modified',
        'seen',
        ]

    ## saved/loaded from ConfigParser instance
    # attributes that aren't in DEFAULT
    _non_default_configured_attributes = [
        'url',
        ]
    # attributes that are in DEFAULT
    _default_configured_attributes = [
        key.replace('-', '_') for key in _config.CONFIG['DEFAULT'].keys()]
    _default_configured_attributes[
        _default_configured_attributes.index('from')
        ] = 'from_email'  # `from` is a Python keyword
    # all attributes that are saved/loaded from .config
    _configured_attributes = (
        _non_default_configured_attributes + _default_configured_attributes)
    # attribute name -> .config option
    _configured_attribute_translations = dict(
        (attr,attr) for attr in _non_default_configured_attributes)
    _configured_attribute_translations.update(dict(
            zip(_default_configured_attributes,
                _config.CONFIG['DEFAULT'].keys())))
    # .config option -> attribute name
    _configured_attribute_inverse_translations = dict(
        (v,k) for k,v in _configured_attribute_translations.items())

    # hints for value conversion
    _boolean_attributes = [
        'force_from',
        'use_publisher_email',
        'friendly_name',
        'active',
        'date_header',
        'trust_guid',
        'html_mail',
        'use_css',
        'unicode_snob',
        'links_after_each_paragraph',
        'use_smtp',
        'smtp_ssl',
        ]

    _integer_attributes = [
        'feed_timeout',
        'body_width',
        ]

    _list_attributes = [
        'date_header_order',
        'encodings',
        ]

    def __init__(self, name=None, url=None, to=None, config=None):
        self._set_name(name=name)
        self.reset()
        self.__setstate__(dict(
                (attr, getattr(self, attr))
                for attr in self._dynamic_attributes))
        self.load_from_config(config=config)
        if url:
            self.url = url
        if to:
            self.to = to

    def __str__(self):
        return '{} ({} -> {})'.format(self.name, self.url, self.to)

    def __repr__(self):
        return '<Feed {}>'.format(str(self))

    def __getstate__(self):
        "Save dyamic attributes"
        return dict(
            (key,getattr(self,key)) for key in self._dynamic_attributes)

    def __setstate__(self, state):
        "Restore dynamic attributes"
        keys = sorted(state.keys())
        if keys != sorted(self._dynamic_attributes):
            raise ValueError(state)
        self._set_name(name=state['name'])
        self.__dict__.update(state)

    def save_to_config(self):
        "Save configured attributes"
        data = _collections.OrderedDict()
        default = self.config['DEFAULT']
        for attr in self._configured_attributes:
            key = self._configured_attribute_translations[attr]
            value = getattr(self, attr)
            if value is not None:
                value = self._get_configured_option_value(
                    attribute=attr, value=value)
                if (attr in self._non_default_configured_attributes or
                    value != default[key]):
                    data[key] = value
        self.config[self.section] = data

    def load_from_config(self, config=None):
        "Restore configured attributes"
        if config is None:
            config = _config.CONFIG
        self.config = config
        if self.section in self.config:
            data = self.config[self.section]
        else:
            data = self.config['DEFAULT']
        keys = sorted(data.keys())
        expected = sorted(self._configured_attribute_translations.values())
        if keys != expected:
            for key in expected:
                if (key not in keys and
                    key not in self._non_default_configured_attributes):
                    raise ValueError('missing key: {}'.format(key))
            for key in keys:
                if key not in expected:
                    raise ValueError('extra key: {}'.format(key))
        data = dict(
            (self._configured_attribute_inverse_translations[k],
             self._get_configured_attribute_value(
                  attribute=self._configured_attribute_inverse_translations[k],
                  key=k, data=data))
            for k in data.keys())
        for attr in self._non_default_configured_attributes:
            if attr not in data:
                data[attr] = None
        self.__dict__.update(data)

    def _get_configured_option_value(self, attribute, value):
        if value and attribute in self._list_attributes:
            return ', '.join(value)
        return str(value)

    def _get_configured_attribute_value(self, attribute, key, data):
        if attribute in self._boolean_attributes:
            return data.getboolean(key)
        elif attribute in self._integer_attributes:
            return data.getint(key)
        elif attribute in self._list_attributes:
            return [x.strip() for x in data[key].split(',')]
        return data[key]

    def reset(self):
        """Reset dynamic data
        """
        self.etag = None
        self.modified = None
        self.seen = {}

    def _set_name(self, name):
        if not self._name_regexp.match(name):
            raise _error.InvalidFeedName(name=name, feed=self)
        self.name = name
        self.section = 'feed.{}'.format(self.name)

    def _fetch(self):
        """Fetch and parse a feed using feedparser.

        >>> feed = Feed(
        ...    name='test-feed',
        ...    url='http://feeds.feedburner.com/allthingsrss/hJBr')
        >>> parsed = feed._fetch()
        >>> parsed.status
        200
        """
        _LOG.info('fetch {}'.format(self))
        if self.section in self.config:
            config = self.config[self.section]
        else:
            config = self.config['DEFAULT']
        proxy = config['proxy']
        timeout = config.getint('feed-timeout')
        kwargs = {}
        if proxy:
            kwargs['handlers'] = [_urllib_request.ProxyHandler({'http':proxy})]
        f = _util.TimeLimitedFunction(timeout, _feedparser.parse)
        return f(self.url, self.etag, modified=self.modified, **kwargs)

    def _process(self, parsed):
        _LOG.info('process {}'.format(self))
        self._check_for_errors(parsed)
        for entry in reversed(parsed.entries):
            _LOG.debug('processing {}'.format(entry.get('id', 'no-id')))
            processed = self._process_entry(parsed=parsed, entry=entry)
            if processed:
                yield processed

    def _check_for_errors(self, parsed):
        warned = False
        status = getattr(parsed, 'status', 200)
        _LOG.debug('HTTP status {}'.format(status))
        if status == 301:
            _LOG.info('redirect {} from {} to {}'.format(
                    self.name, self.url, parsed['url']))
            self.url = parsed['url']
        elif status not in [200, 302, 304]:
            raise _error.HTTPError(status=status, feed=self)

        http_headers = parsed.get('headers', {})
        if http_headers:
            _LOG.debug('HTTP headers: {}'.format(http_headers))
        if not http_headers:
            _LOG.warning('could not get HTTP headers: {}'.format(self))
            warned = True
        else:
            if 'html' in http_headers.get('content-type', 'rss'):
                _LOG.warning('looks like HTML: {}'.format(self))
                warned = True
            if http_headers.get('content-length', '1') == '0':
                _LOG.warning('empty page: {}'.format(self))
                warned = True

        version = parsed.get('version', None)
        if version:
            _LOG.debug('feed version {}'.format(version))
        else:
            _LOG.warning('unrecognized version: {}'.format(self))
            warned = True

        exc = parsed.get('bozo_exception', None)
        if isinstance(exc, _socket.timeout):
            _LOG.error('timed out: {}'.format(self))
            warned = True
        elif isinstance(exc, _SOCKET_ERRORS):
            reason = exc.args[1]
            _LOG.error('{}: {}'.format(exc, self))
            warned = True
        elif (hasattr(exc, 'reason') and
              isinstance(exc.reason, _urllib_error.URLError)):
            if isinstance(exc.reason, _SOCKET_ERRORS):
                reason = exc.reason.args[1]
            else:
                reason = exc.reason
            _LOG.error('{}: {}'.format(exc, self))
            warned = True
        elif isinstance(exc, _feedparser.zlib.error):
            _LOG.error('broken compression: {}'.format(self))
            warned = True
        elif isinstance(exc, (IOError, AttributeError)):
            _LOG.error('{}: {}'.format(exc, self))
            warned = True
        elif isinstance(exc, KeyboardInterrupt):
            raise exc
        elif isinstance(exc, _sax.SAXParseException):
            _LOG.error('sax parsing error: {}: {}'.format(exc, self))
            warned = True
        elif parsed.bozo or exc:
            if exc is None:
                exc = "can't process"
            _LOG.error('processing error: {}: {}'.format(exc, self))
            warned = True

        if (not warned and
            status in [200, 302] and
            not parsed.entries and
            not version):
            raise _error.ProcessingError(parsed=parsed, feed=feed)

    def _process_entry(self, parsed, entry):
        id_ = self._get_entry_id(entry)
        # If .trust_guid isn't set, we get back hashes of the content.
        # Instead of letting these run wild, we put them in context
        # by associating them with the actual ID (if it exists).
        guid = entry['id'] or id_
        if isinstance(guid, dict):
            guid = guid.values()[0]
        if guid in self.seen:
            if self.seen[guid] == id_:
                _LOG.debug('already seen {}'.format(id_))
                return  # already seen
        sender = self._get_entry_email(parsed=parsed, entry=entry)
        link = entry.get('link', None)
        subject = self._get_entry_title(entry)
        extra_headers = _collections.OrderedDict((
                ('Date', self._get_entry_date(entry)),
                ('Message-ID', '<{}@dev.null.invalid>'.format(_uuid.uuid4())),
                ('User-Agent', 'rss2email'),
                ('X-RSS-Feed', self.url),
                ('X-RSS-ID', id_),
                ('X-RSS-URL', link),
                ('X-RSS-TAGS', self._get_entry_tags(entry)),
                ))
        for k,v in extra_headers.items():  # remove empty tags, etc.
            if v is None:
                extra_headers.pop(k)
        if self.bonus_header:
            for header in self.bonus_header.splitlines():
                if ':' in header:
                    key,value = header.split(':', 1)
                    extra_headers[key.strip()] = value.strip()
                else:
                    _LOG.warning(
                        'malformed bonus-header: {}'.format(
                            self.bonus_header))

        content = self._get_entry_content(entry)
        content = self._process_entry_content(
            entry=entry, content=content, link=link, subject=subject)
        message = _email.get_message(
            sender=sender,
            recipient=self.to,
            subject=subject,
            body=content['value'],
            content_type=content['type'].split('/', 1)[1],
            extra_headers=extra_headers)
        return (guid, id_, sender, message)

    def _get_entry_id(self, entry):
        """Get best ID from an entry."""
        if self.trust_guid:
            if getattr(entry, 'id', None):
                # Newer versions of feedparser could return a dictionary
                if isinstance(entry.id, dict):
                    return entry.id.values()[0]
                return entry.id
        content_type,content_value = self._get_entry_content(entry)
        content_value = content_value.strip()
        if content_value:
            return hash(content_value.encode('unicode-escape')).hexdigest()
        elif getattr(entry, 'link', None):
            return hash(entry.link.encode('unicode-escape')).hexdigest()
        elif getattr(entry, 'title', None):
            return hash(entry.title.encode('unicode-escape')).hexdigest()

    def _get_entry_title(self, entry):
        if hasattr(entry, 'title_detail') and entry.title_detail:
            title = entry.title_detail.value
            if 'html' in entry.title_detail.type:
                title = _html2text.html2text(title)
        else:
            title = self._get_entry_content(entry).content[:70]
        title = title.replace('\n', ' ').strip()
        return title

    def _get_entry_date(self, entry):
        datetime = _time.gmtime()
        if self.date_header:
            for datetype in self.date_header_order:
                kind = datetype + '_parsed'
                if entry.get(kind, None):
                    datetime = entry[kind]
                    break
        return _time.strftime("%a, %d %b %Y %H:%M:%S -0000", datetime)

    def _get_entry_name(self, parsed, entry):
        """Get the best name

        >>> import feedparser
        >>> f = Feed(name='test-feed')
        >>> parsed = feedparser.parse(
        ...     '<feed xmlns="http://www.w3.org/2005/Atom">\\n'
        ...     '  <entry>\\n'
        ...     '    <author>\\n'
        ...     '      <name>Example author</name>\\n'
        ...     '      <email>me@example.com</email>\\n'
        ...     '      <url>http://example.com/</url>\\n'
        ...     '    </author>\\n'
        ...     '  </entry>\\n'
        ...     '</feed>\\n'
        ...     )
        >>> entry = parsed.entries[0]
        >>> f.friendly_name = False
        >>> f._get_entry_name(parsed, entry)
        ''
        >>> f.friendly_name = True
        >>> f._get_entry_name(parsed, entry)
        'Example author'
        """
        if not self.friendly_name:
            return ''
        parts = ['']
        feed = parsed.feed
        parts.append(feed.get('title', ''))
        for x in [entry, feed]:
            if 'name' in x.get('author_detail', []):
                if x.author_detail.name:
                    if ''.join(parts):
                        parts.append(': ')
                    parts.append(x.author_detail.name)
                    break
        if not ''.join(parts) and self.use_publisher_email:
            if 'name' in feed.get('publisher_detail', []):
                if ''.join(parts):
                    parts.append(': ')
                parts.append(feed.publisher_detail.name)
        return _html2text.unescape(''.join(parts))

    def _validate_email(self, email, default=None):
        """Do a basic quality check on email address

        Return `default` if the address doesn't appear to be
        well-formed.  If `default` is `None`, return
        `self.from_email`.

        >>> f = Feed(name='test-feed')
        >>> f._validate_email('valid@example.com', 'default@example.com')
        'valid@example.com'
        >>> f._validate_email('invalid@', 'default@example.com')
        'default@example.com'
        >>> f._validate_email('@invalid', 'default@example.com')
        'default@example.com'
        >>> f._validate_email('invalid', 'default@example.com')
        'default@example.com'
        """
        parts = email.split('@')
        if len(parts) != 2 or '' in parts:
            if default is None:
                return self.from_email
            return default
        return email

    def _get_entry_address(self, parsed, entry):
        """Get the best From email address ('<jdoe@a.com>')

        If the best guess isn't well-formed (something@somthing.com),
        use `self.from_email` instead.
        """
        if self.force_from:
            return self.from_email
        feed = parsed.feed
        if 'email' in entry.get('author_detail', []):
            return self._validate_email(entry.author_detail.email)
        elif 'email' in feed.get('author_detail', []):
            return self._validate_email(feed.author_detail.email)
        if self.use_publisher_email:
            if 'email' in feed.get('publisher_detail', []):
                return self._validate_email(feed.publisher_detail.email)
            if feed.get('errorreportsto', None):
                return self._validate_email(feed.errorreportsto)
        _LOG.debug('no sender address found, fallback to default')
        return self.from_email

    def _get_entry_email(self, parsed, entry):
        """Get the best From email address ('John <jdoe@a.com>')
        """
        name = self._get_entry_name(parsed=parsed, entry=entry)
        address = self._get_entry_address(parsed=parsed, entry=entry)
        return _formataddr((name, address))

    def _get_entry_tags(self, entry):
        """Add post tags, if available

        >>> f = Feed(name='test-feed')
        >>> f._get_entry_tags({
        ...         'tags': [{'term': 'tag1',
        ...                   'scheme': None,
        ...                   'label': None}]})
        'tag1'
        >>> f._get_entry_tags({
        ...         'tags': [{'term': 'tag1',
        ...                   'scheme': None,
        ...                   'label': None},
        ...                  {'term': 'tag2',
        ...                   'scheme': None,
        ...                   'label': None}]})
        'tag1,tag2'

        Test some troublesome cases.  No tags:

        >>> f._get_entry_tags({})

        Empty tags:

        >>> f._get_entry_tags({'tags': []})

        Tags without a ``term`` entry:

        >>> f._get_entry_tags({
        ...         'tags': [{'scheme': None,
        ...                   'label': None}]})

        Tags with an empty term:

        >>> f._get_entry_tags({
        ...         'tags': [{'term': '',
        ...                   'scheme': None,
        ...                   'label': None}]})
        """
        taglist = [tag['term'] for tag in entry.get('tags', [])
                   if tag.get('term', '')]
        if taglist:
            return ','.join(taglist)

    def _get_entry_content(self, entry):
        """Select the best content from an entry.

        Returns a feedparser content dict.
        """
        # How this works:
        #  * We have a bunch of potential contents.
        #  * We go thru looking for our first choice.
        #    (HTML or text, depending on self.html_mail)
        #  * If that doesn't work, we go thru looking for our second choice.
        #  * If that still doesn't work, we just take the first one.
        #
        # Possible future improvement:
        #  * Instead of just taking the first one
        #    pick the one in the "best" language.
        #  * HACK: hardcoded .html_mail, should take a tuple of media types
        contents = list(entry.get('content', []))
        if entry.get('summary_detail', None):
            contents.append(entry.summary_detail)
        if self.html_mail:
            types = ['text/html', 'text/plain']
        else:
            types = ['text/plain', 'text/html']
        for content_type in types:
            for content in contents:
                if content['type'] == content_type:
                    return content
        if contents:
            return contents[0]
        return {type: 'text/plain', 'value': ''}

    def _process_entry_content(self, entry, content, link, subject):
        "Convert entry content to the requested format."
        if self.html_mail:
            lines = [
                '<!DOCTYPE html>',
                '<html>',
                '  <head>',
                ]
            if self.use_css and self.css:
                lines.extend([
                        '    <style type="text/css">',
                        self.css,
                        '    </style>',
                        ])
            lines.extend([
                    '</head>',
                    '<body>',
                    '<div id="entry>',
                    '<h1 class="header"><a href="{}">{}</a></h1>'.format(
                        link, subject),
                    '<div id="body"><table><tr><td>',
                    ])
            if content['type'] in ('text/html', 'application/xhtml+xml'):
                lines.append(content['value'].strip())
            else:
                lines.append(_saxutils.escape(content['value'].strip()))
            lines.append('</td></tr></table></div>')
            lines.extend([
                    '<div class="footer">'
                    '<p>URL: <a href="{0}">{0}</a></p>'.format(link),
                    ])
            for enclosure in getattr(entry, 'enclosures', []):
                if getattr(enclosure, 'url', None):
                    lines.append(
                        '<p>Enclosure: <a href="{0}">{0}</a></p>'.format(
                            enclosure.url))
                if getattr(enclosure, 'src', None):
                    lines.append(
                        '<p>Enclosure: <a href="{0}">{0}</a></p>'.format(
                            enclosure.src))
                    lines.append(
                        '<p><img src="{}" /></p>'.format(enclosure.src))
            for elink in getattr(entry, 'links', []):
                if elink.get('rel', None) == 'via':
                    url = elink['href']
                    url = url.replace(
                        'http://www.google.com/reader/public/atom/',
                        'http://www.google.com/reader/view/')
                    title = url
                    if elink.get('title', None):
                        title = elink['title']
                    lines.append('<p>Via <a href="{}">{}</a></p>'.format(
                            url, title))
            lines.extend([
                    '</div>',  # /footer
                    '</div>',  # /entry
                    '</body>',
                    '</html>',
                    ''])
            content['type'] = 'text/html'
            content['value'] = '\n'.join(lines)
            return content
        else:  # not self.html_mail
            if content['type'] in ('text/html', 'application/xhtml+xml'):
                lines = [_html2text.html2text(content['value'])]
            else:
                lines = [content['value']]
            lines.append('')
            lines.append('URL: {}'.format(link))
            for enclosure in getattr(entry, 'enclosures', []):
                if getattr(enclosure, 'url', None):
                    lines.append('Enclosure: {}'.format(enclosure.url))
                if getattr(enclosure, 'src', None):
                    lines.append('Enclosure: {}'.format(enclosure.src))
            for elink in getattr(entry, 'links', []):
                if elink.get('rel', None) == 'via':
                    url = elink['href']
                    url = url.replace(
                        'http://www.google.com/reader/public/atom/',
                        'http://www.google.com/reader/view/')
                    title = url
                    if elink.get('title', None):
                        title = elink['title']
                    lines.append('Via: {} {}'.format(title, url))
            content['type'] = 'text/plain'
            content['value'] = '\n'.join(lines)
            return content

    def _send(self, sender, message):
        _LOG.info('send message for {}'.format(self))
        section = self.section
        if section not in self.config:
            section = 'DEFAULT'
        _email.send(sender=sender, recipient=self.to, message=message,
                    config=self.config, section=section)

    def run(self, send=True):
        """Fetch and process the feed, mailing entry emails.

        >>> feed = Feed(
        ...    name='test-feed',
        ...    url='http://feeds.feedburner.com/allthingsrss/hJBr')
        >>> def send(sender, message):
        ...    print('send from {}:'.format(sender))
        ...    print(message.as_string())
        >>> feed._send = send
        >>> feed.to = 'jdoe@dummy.invalid'
        >>> #parsed = feed.run()  # enable for debugging
        """
        if not self.to:
            raise _error.NoToEmailAddress(feed=self)
        parsed = self._fetch()
        for (guid, id_, sender, message) in self._process(parsed):
            _LOG.debug('new message: {}'.format(message['Subject']))
            if send:
                self._send(sender=sender, message=message)
            self.seen[guid] = id_
        self.etag = parsed.get('etag', None)
        self.modified = parsed.get('modified', None)
