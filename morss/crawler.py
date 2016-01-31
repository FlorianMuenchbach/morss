import sys

import ssl
import socket

from gzip import GzipFile
from io import BytesIO, StringIO
import re
import sqlite3
import time

try:
    from urllib2 import BaseHandler, Request, addinfourl, parse_keqv_list, parse_http_list
    import mimetools
except ImportError:
    from urllib.request import BaseHandler, Request, addinfourl, parse_keqv_list, parse_http_list
    import email

try:
    basestring
except NameError:
    basestring = unicode = str
    buffer = memoryview


MIMETYPE = {
    'xml': ['text/xml', 'application/xml', 'application/rss+xml', 'application/rdf+xml', 'application/atom+xml'],
    'html': ['text/html', 'application/xhtml+xml', 'application/xml']}

class GZIPHandler(BaseHandler):
    def http_request(self, req):
        req.add_unredirected_header('Accept-Encoding', 'gzip')
        return req

    def http_response(self, req, resp):
        if 200 <= resp.code < 300:
            if resp.headers.get('Content-Encoding') == 'gzip':
                data = resp.read()
                data = GzipFile(fileobj=BytesIO(data), mode='r').read()

                fp = BytesIO(data)
                old_resp = resp
                resp = addinfourl(fp, old_resp.headers, old_resp.url, old_resp.code)
                resp.msg = old_resp.msg

        return resp

    https_response = http_response
    https_request = http_request


def detect_encoding(data, con=None):
    if con is not None and con.info().get('charset'):
        return con.info().get('charset')

    match = re.search(b'charset=["\']?([0-9a-zA-Z-]+)', data[:1000])
    if match:
        return match.groups()[0].lower().decode()

    match = re.search(b'encoding=["\']?([0-9a-zA-Z-]+)', data[:100])
    if match:
        return match.groups()[0].lower().decode()

    return 'utf-8'


class EncodingFixHandler(BaseHandler):
    def __init__(self, encoding_ovrride):
        self.encoding_ovrride = encoding_ovrride

    def http_response(self, req, resp):
        maintype = resp.info().get('Content-Type', '').split('/')[0]
        if 200 <= resp.code < 300 and maintype == 'text':
            data = resp.read()
            enc = detect_encoding(data, resp) if not self.encoding_ovrride else self.encoding_ovrride

            if enc:
                data = data.decode(enc, 'replace')
                data = data.encode(enc)

            fp = BytesIO(data)
            old_resp = resp
            resp = addinfourl(fp, old_resp.headers, old_resp.url, old_resp.code)
            resp.msg = old_resp.msg

        return resp

    https_response = http_response


class UAHandler(BaseHandler):
    def __init__(self, useragent=None):
        self.useragent = useragent

    def http_request(self, req):
        if self.useragent:
            req.add_unredirected_header('User-Agent', self.useragent)
        return req

    https_request = http_request


class AutoRefererHandler(BaseHandler):
    def http_request(self, req):
        if req.host != 'feeds.feedburner.com':
            req.add_unredirected_header('Referer', 'http://%s' % req.host)
        return req

    https_request = http_request


class ContentNegociationHandler(BaseHandler): #FIXME
    def __init__(self, accept=None, strict=False):
        self.accept = accept
        self.strict = strict

    def http_request(self, req):
        if self.accept is not None:
            if isinstance(self.accept, basestring):
                self.accept = (self.accept,)

            out = {}
            rank = 1.1
            for group in self.accept:
                rank -= 0.1

                if isinstance(group, basestring):
                    if group in MIMETYPE:
                        group = MIMETYPE[group]
                    else:
                        out[group] = rank
                        continue

                for mime in group:
                    if mime not in out:
                        out[mime] = rank

            if not self.strict:
                out['*/*'] = rank - 0.1

            string = ','.join([x + ';q={0:.1}'.format(out[x]) if out[x] != 1 else x for x in out])
            req.add_unredirected_header('Accept', string)

        return req

    https_request = http_request


class HTTPEquivHandler(BaseHandler):
    " Handler to support <meta http-equiv='...' content='...' />, since it defines HTTP headers "

    handler_order = 600

    def http_response(self, req, resp):
        contenttype = resp.info().get('Content-Type', '').split(';')[0]
        if 200 <= resp.code < 300 and contenttype.startswith('text/'):
            if contenttype in MIMETYPE['html']:
                data = resp.read()

                regex = r'(?i)<meta\s+http-equiv=(["\'])(?P<key>[^"\']+)\1\s+content=(["\'])(?P<value>[^>]+)\3\s*/?>'
                headers = [x.groupdict() for x in re.finditer(regex, data[:1000].decode('utf-8', 'replace'))]

                for header in headers:
                    resp.headers[header['key'].lower()] = header['value']

                fp = BytesIO(data)
                old_resp = resp
                resp = addinfourl(fp, old_resp.headers, old_resp.url, old_resp.code)
                resp.msg = old_resp.msg

        return resp

    https_response = http_response


class HTTPRefreshHandler(BaseHandler):
    handler_order = 700 # HTTPErrorProcessor has a handler_order of 1000

    def http_response(self, req, resp):
        if 200 <= resp.code < 300:
            if resp.headers.get('refresh'):
                regex = r'(?i)^(?P<delay>[0-9]+)\s*;\s*url=(["\']?)(?P<url>.+)\2$'
                match = re.search(regex, resp.headers.get('refresh'))

                if match:
                    url = match.groupdict()['url']

                    if url:
                        resp.code = 302
                        resp.msg = 'Moved Temporarily'
                        resp.headers['location'] = url

        return resp

    https_response = http_response


class NotInCache(IOError):
    pass


class BaseCacheHandler(BaseHandler):
    " Cache based on etags/last-modified. Inherit from this to implement actual storage "

    private_cache = False # False to behave like a CDN (or if you just don't care), True like a PC
    handler_order = 499

    def __init__(self, force_min=None):
        self.force_min = force_min # force_min (seconds) to bypass http headers, -1 forever, 0 never, -2 do nothing if not in cache

    def _load(self, url):
        out = list(self.load(url))

        if sys.version_info[0] >= 3:
            out[2] = email.message_from_string(out[2] or unicode()) # headers
        else:
            out[2] = mimetools.Message(StringIO(out[2] or unicode()))

        out[3] = out[3] or bytes() # data
        out[4] = out[4] or 0 # timestamp

        return out

    def load(self, url):
        " Return the basic vars (code, msg, headers, data, timestamp) "
        return (None, None, None, None, None)

    def _save(self, url, code, msg, headers, data, timestamp):
        headers = unicode(headers)
        self.save(url, code, msg, headers, data, timestamp)

    def save(self, url, code, msg, headers, data, timestamp):
        " Save values to disk "
        pass

    def http_request(self, req):
        (code, msg, headers, data, timestamp) = self._load(req.get_full_url())

        if 'etag' in headers:
            req.add_unredirected_header('If-None-Match', headers['etag'])

        if 'last-modified' in headers:
            req.add_unredirected_header('If-Modified-Since', headers.get('last-modified'))

        return req

    def http_open(self, req):
        (code, msg, headers, data, timestamp) = self._load(req.get_full_url())

        # some info needed to process everything
        cache_control = parse_http_list(headers.get('cache-control', ()))
        cache_control += parse_http_list(headers.get('pragma', ()))

        cc_list = [x for x in cache_control if '=' not in x]
        cc_values = parse_keqv_list([x for x in cache_control if '=' in x])

        cache_age = time.time() - timestamp

        # list in a simple way what to do when
        if req.get_header('Morss') == 'from_304': # for whatever reason, we need an uppercase
            # we're just in the middle of a dirty trick, use cache
            pass

        elif self.force_min == -2:
            if code is not None:
                # already in cache, perfect, use cache
                pass

            else:
                headers['Morss'] = 'from_cache'
                resp = addinfourl(BytesIO(), headers, req.get_full_url(), 409)
                resp.msg = 'Conflict'
                return resp

        elif code is None:
            # cache empty, refresh
            return None

        elif self.force_min == -1:
            # force use cache
            pass

        elif self.force_min == 0:
            # force refresh
            return None

        elif  self.force_min is None and ('no-cache' in cc_list
                                        or 'no-store' in cc_list
                                        or ('private' in cc_list and not self.private)):
            # kindly follow web servers indications, refresh
            return None

        elif 'max-age' in cc_values and int(cc_values['max-age']) > cache_age:
            # server says it's still fine (and we trust him, if not, use force_min=0), use cache
            pass

        elif self.force_min is not None and self.force_min > cache_age:
            # still recent enough for us, use cache
            pass

        else:
            # according to the www, we have to refresh when nothing is said
            return None

        # return the cache as a response
        headers['morss'] = 'from_cache' # TODO delete the morss header from incoming pages, to avoid websites messing up with us
        resp = addinfourl(BytesIO(data), headers, req.get_full_url(), code)
        resp.msg = msg

        return resp

    def http_response(self, req, resp):
        # code for after-fetch, to know whether to save to hard-drive (if stiking to http headers' will)

        if resp.code == 304:
            return resp

        if ('cache-control' in resp.headers or 'pragma' in resp.headers) and self.force_min is None:
            cache_control = parse_http_list(resp.headers.get('cache-control', ()))
            cache_control += parse_http_list(resp.headers.get('pragma', ()))

            cc_list = [x for x in cache_control if '=' not in x]

            if 'no-cache' in cc_list or 'no-store' in cc_list or ('private' in cc_list and not self.private):
                # kindly follow web servers indications
                return resp

        if resp.headers.get('Morss') == 'from_cache':
            # it comes from cache, so no need to save it again
            return resp

        # save to disk
        data = resp.read()
        self._save(req.get_full_url(), resp.code, resp.msg, resp.headers, data, time.time())

        fp = BytesIO(data)
        old_resp = resp
        resp = addinfourl(fp, old_resp.headers, old_resp.url, old_resp.code)
        resp.msg = old_resp.msg

        return resp

    def http_error_304(self, req, fp, code, msg, headers):
        cache = list(self._load(req.get_full_url()))

        if cache[0]:
            cache[-1] = time.time()
            self._save(req.get_full_url(), *cache)

            new = Request(req.get_full_url(),
                           headers=req.headers,
                           unverifiable=True)

            new.add_unredirected_header('Morss', 'from_304')

            return self.parent.open(new, timeout=req.timeout)

        return None

    https_request = http_request
    https_open = http_open
    https_response = http_response


sqlite_default = ':memory'


class SQliteCacheHandler(BaseCacheHandler):
    def __init__(self, force_min=-1, filename=None):
        BaseCacheHandler.__init__(self, force_min)

        self.con = sqlite3.connect(filename or sqlite_default, detect_types=sqlite3.PARSE_DECLTYPES, check_same_thread=False)
        self.con.execute('create table if not exists data (url unicode PRIMARY KEY, code int, msg unicode, headers unicode, data bytes, timestamp int)')
        self.con.commit()

    def __del__(self):
        self.con.close()

    def load(self, url):
        row = self.con.execute('select * from data where url=?', (url,)).fetchone()

        if not row:
            return (None, None, None, None, None)

        return row[1:]

    def save(self, url, code, msg, headers, data, timestamp):
        data = buffer(data)

        if self.con.execute('select code from data where url=?', (url,)).fetchone():
            self.con.execute('update data set code=?, msg=?, headers=?, data=?, timestamp=? where url=?',
                (code, msg, headers, data, timestamp, url))

        else:
            self.con.execute('insert into data values (?,?,?,?,?,?)', (url, code, msg, headers, data, timestamp))

        self.con.commit()
