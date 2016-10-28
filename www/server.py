#!/usr/bin/env python

import sys
import os
import logging
import urlparse
import urllib
import codecs

import flask
import werkzeug
import werkzeug.security
from werkzeug.contrib.fixers import ProxyFix
from werkzeug.datastructures import Headers
# from flask.ext.cors import cross_origin

import time
import random

import requests
import json

# helpful for figuring out headers aren't being set...
# logging.getLogger('flask_cors').level = logging.DEBUG

# http://flask.pocoo.org/snippets/35/

class ES:

    # see also : https://github.com/mapzen/mapzen-www-places/issues/5

    def __init__ (self, **kwargs):

        self.host = kwargs.get('host', 'localhost')
        self.port = kwargs.get('port', 9200)
        self.index = kwargs.get('index', None)

        self.per_page = kwargs.get('per_page', 100)
        self.per_page_max = kwargs.get('per_page_max', 500)

        self.page = 1
        
    def __str__ (self):
        return "%s:%s (%s)" % (self.host, self.port, self.index)

    def query(self, **kwargs) :

        path = kwargs.get('path', '_search')
        body = kwargs.get('body', {})
        query = kwargs.get('query', {})

        if self.index:
            url = "http://%s:%s/%s/%s" % (self.host, self.port, self.index, path)
        else:
            url = "http://%s:%s/%s" % (self.host, self.port, path)

        page = self.page
        per_page = self.per_page

        if query.get('per_page', None):

            per_page = query['per_page']
            del(query['per_page'])

            if per_page > self.per_page_max:
                per_page = self.per_page_max

        if query.get('page', None):
            page = query['page']
            del(query['page'])

        query['_from'] = (page - 1) * per_page
        query['size'] = per_page

        if len(query.keys()):
            q = urllib.urlencode(query)
            url = url + "?" + q
            
        body = json.dumps(body)

        rsp = requests.post(url, data=body)
        return json.loads(rsp.content)

    def single(self, rsp):

        count = len(rsp['hits']['hits'])

        if count == 0:
            return None

        if count > 1:
            logging.warning("invoking single on a result set with %s results" % count)
            return None

        return rsp['hits']['hits'][0]

    def paginate(self, rsp, **kwargs):

        per_page = kwargs.get('per_page', self.per_page)

        if per_page > self.per_page_max:
            per_page = self.per_page_max

        page = kwargs.get('page', self.page)

        hits = rsp['hits']
        total = hits['total']

        docs = hits['hits']
        count = len(docs)

        pages = float(total) / float(per_page)
        pages = math.ceil(pages)
        pages = int(pages)

        pagination = {
            'total': total,
            'count': count,
            'per_page': per_page,
            'page': page,
            'pages': pages
        }

        return pagination

class ReverseProxied(object):
    '''Wrap the application in this middleware and configure the 
    front-end server to add these headers, to let you quietly bind 
    this to a URL other than / and to an HTTP scheme that is 
    different than what is used locally.

    In nginx:
    location /myprefix {
        proxy_pass http://192.168.0.1:5001;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Scheme $scheme;
        proxy_set_header X-Proxy-Path /myprefix;
        }

    :param app: the WSGI application
    '''
    def __init__(self, app):
        self.app = app

    def __call__(self, environ, start_response):
        script_name = environ.get('HTTP_X_PROXY_PATH', '')
        if script_name:
            environ['SCRIPT_NAME'] = script_name
            path_info = environ['PATH_INFO']
            if path_info.startswith(script_name):
                environ['PATH_INFO'] = path_info[len(script_name):]

        scheme = environ.get('HTTP_X_SCHEME', '')
        if scheme:
            environ['wsgi.url_scheme'] = scheme
        return self.app(environ, start_response)

app = flask.Flask('MAPZEN_PLACES')
app.wsgi_app = ProxyFix(app.wsgi_app)
app.wsgi_app = ReverseProxied(app.wsgi_app)

logging.basicConfig(level=logging.INFO)

@app.before_request
def init():

    search_host = os.environ.get('PLACES_SEARCH_HOST', None)
    search_port = os.environ.get('PLACES_SEARCH_PORT', None)
    search_index = os.environ.get('PLACES_SEARCH_INDEX', 'whosonfirst')

    es = ES(host=search_host, port=search_port, index=search_index)
    flask.g.es = es
    
    pass

@app.template_filter()
def urlencode(value):
    s = unicode(value)
    return urllib.quote(s)

# http://flask.pocoo.org/snippets/29/

@app.template_filter()
def number_format(value, tsep=',', dsep='.'):

    s = unicode(value)

    cnt = 0
    numchars = dsep + '0123456789'

    ls = len(s)

    while cnt < ls and s[cnt] not in numchars:
        cnt += 1

    lhs = s[:cnt]
    s = s[cnt:]

    if not dsep:
        cnt = -1
    else:
        cnt = s.rfind(dsep)

    if cnt > 0:
        rhs = dsep + s[cnt+1:]
        s = s[:cnt]
    else:
        rhs = ''

    splt = ''

    while s != '':
        splt = s[-3:] + tsep + splt
        s = s[:-3]

    return lhs + splt[:-1] + rhs

@app.errorhandler(404)
def page_not_found(e):
    return flask.render_template('404.html'), 404

@app.errorhandler(500)
def server_error(e):
    return flask.render_template('500.html'), 500

@app.route("/500", methods=["GET"])
@app.route("/500/", methods=["GET"])
def server_error():
    flask.abort(500)

# Sitemaps are actually generated out of band, cloned to an S3
# bucket and handled by a proxy handler upstream. See also:
# https://github.com/whosonfirst/whosonfirst-sitemaps
# (20161027/thisisaaronland)

@app.route('/sitemap.xml', methods=["GET"])
@app.route('/sitemaps.xml', methods=["GET"])
def sitemap_xml():

    location = flask.url_for('index')
    location = os.path.join(location, "sitemaps/index.xml")

    return flask.redirect(location, code=303)

@app.route('/robots.txt')
def robots_txt():

    headers = Headers()
    headers.add("Content-type", "text/plain")

    body = flask.render_template('robots.txt')
    return flask.Response(body, headers=headers)

@app.route("/", methods=["GET"])
def index():
    return flask.render_template('index.html')

@app.route("/id", methods=["GET"])
@app.route("/id/", methods=["GET"])
def null_id():
    location = flask.url_for('index')
    return flask.redirect(location, code=303)

@app.route("/id/<int:id>", methods=["GET"])
@app.route("/id/<int:id>/", methods=["GET"])
def place_id(id):

    query = {
        'ids': {
            'values': [id]
        }
    }
    
    body = {
        'query': query
    }
    
    rsp = flask.g.es.query(body=body)
    doc = flask.g.es.single(rsp)

    if not doc:
        flask.abort(400)

    place = doc2geojson(doc)

    return flask.render_template('id.html', place=place)

@app.route("/random", methods=["GET"])
@app.route("/random/", methods=["GET"])
def random_place():

    now = time.time()
    now = int(now)

    seed = random.randint(0, now)

    es_query = {
        'function_score': {
            'query': {
                'match_all' : { }
            },
            'functions': [
                { 'random_score': { 'seed': seed } }
            ]
        }
    }

    body = {
        'query': es_query
    }
    
    query = {
        'per_page': 1
    }

    rsp = flask.g.es.query(body=body, query=query)
    doc = flask.g.es.single(rsp)

    if not doc:
        flask.abort(404)

    location = flask.url_for('place_id', id=doc['_id'])
    return flask.redirect(location, code=303)

@app.route("/random/<placetype>", methods=["GET"])
@app.route("/random/<placetype>/", methods=["GET"])
def random_placetype(placetype):

    # this doesn't work yet...
    # https://github.com/mapzen/mapzen-www-places/issues/11

    flask.abort(404)

    """
    placetype = sanitize_str(placetype)

    if not pt.is_valid_placetype(placetype) and placetype != 'airport':
        flask.abort(404)
    """

    query = {
        'term': {
            'wof:placetype': placetype
        }
    }

    if placetype == 'airport':

        query = {'filtered': {
            'filter': { 'term': { 'wof:category': 'airport' } },
            'query': { 'term': { 'wof:placetype': 'campus' } }
        }}

    now = time.time()
    now = int(now)

    seed = random.randint(0, now)

    es_query = {
        'function_score': {
            'query': query,
            'functions': [
                { 'random_score': { 'seed': seed } }
            ]
        }
    }
    
    body = {
        'query': es_query,
    }

    rsp = flask.g.es.query(body=body, query=query)
    doc = flask.g.es.single(rsp)

    if not doc:
        flask.abort(404)

    location = flask.url_for('place_id', id=doc['_id'])
    return flask.redirect(location, code=303)

def doc2geojson(doc):

    return {
        'type': 'Feature',
        'properties': doc['_source'],
        'geoemtry': {}		# see that... we'll figure it out (20161027/thisisaaronland)
    }
