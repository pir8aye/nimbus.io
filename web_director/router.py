import os
from functools import wraps
import time
import logging
from collections import deque
import gevent
from gevent.coros import RLock as Lock
from gevent.event import Event
import httplib
from redis import StrictRedis, RedisError
import socket
import json

import psycopg2

from tools.LRUCache import LRUCache
from tools.database_connection import retry_central_connection

# LRUCache mapping names to integers is approximately 32m of memory per 100,000
# entries

# how long to wait before returning an error message to avoid fast loops
RETRY_DELAY = 1.0
AVAILABILITY_TIMEOUT = 30.0

COLLECTION_CACHE_SIZE = 500000

NIMBUS_IO_SERVICE_DOMAIN = os.environ['NIMBUS_IO_SERVICE_DOMAIN']
NIMBUSIO_WEB_SERVER_PORT = int(os.environ['NIMBUSIO_WEB_SERVER_PORT'])
NIMBUSIO_WEB_WRITER_PORT = int(os.environ['NIMBUSIO_WEB_WRITER_PORT'])
NIMBUSIO_MANAGEMENT_API_REQUEST_DEST = \
    os.environ['NIMBUSIO_MANAGEMENT_API_REQUEST_DEST']

REDIS_HOST = os.environ.get("REDIS_HOST", "localhost")
REDIS_PORT = int(os.environ.get("REDIS_PORT", str(6379)))
REDIS_DB = int(os.environ.get("REDIS_DB", str(0)))
REDIS_WEB_MONITOR_HASH_NAME = "nimbus.io.web_monitor.{0}".format(
    socket.gethostname())
REDIS_WEB_MONITOR_HASHKEY_FORMAT = "%s:%s"

def _supervise_db_interaction(bound_method):
    """
    Decorator for methods of Router class (below) to manage locks and
    reconnections to database
    """
    @wraps(bound_method)
    def __supervise_db_interaction(instance, *args, **kwargs):
        log = logging.getLogger("supervise_db")
        lock = instance.dblock
        retries = 0
        start_time = time.time()

        # it maybe that some other greenlet has got here first, and already
        # updated our cache to include the item that we are querying. In some
        # situations, such as when the database takes a few seconds to respond,
        # or when the database is offline, there maybe many greenlets waiting,
        # all to query the database for the same result.  To avoid this
        # thundering herd of database hits of likely cached values, the caller
        # may supply us with a cache check function.
        cache_check_func = None
        if 'cache_check_func' in kwargs:
            cache_check_func = kwargs.pop('cache_check_func') 

        while True:
            if retries:
                # do not retry too fast
                time.sleep(1.0)
            with lock:
                conn_id = id(instance.conn)
                try:
                    if cache_check_func is not None:
                        result = cache_check_func()
                        if result:
                            break
                    result = bound_method(instance, *args, **kwargs)
                    break
                except psycopg2.OperationalError, err:
                    log.warn("Database error %s %s (retry #%d)" % (
                        getattr(err, "pgcode", '-'),
                        getattr(err, "pgerror", '-'),
                        retries, ))
                    retries += 1
                    # only let one greenlet be retrying the connection
                    # only reconnect if some other greenlet hasn't already done
                    # so.
                    log.warn("replacing database connection %r" % (
                        conn_id, ))
                    try:
                        if instance.conn is not None:
                            instance.conn.close()
                    except psycopg2.OperationalError, err2:
                        pass
                    instance.conn = retry_central_connection(
                        isolation_level =
                            psycopg2.extensions.ISOLATION_LEVEL_AUTOCOMMIT)
                    conn_id = id(instance.conn)
        return result
    return __supervise_db_interaction

class Router(object):
    """
    Router object for assisting the proxy function (below.)
    Holds database connection, state for caching, etc.
    """

    def __init__(self):
        self.init_complete = Event()
        self.conn = None
        self.redis = None
        self.dblock = Lock()
        self.service_domain = NIMBUS_IO_SERVICE_DOMAIN
        self.read_dest_port = NIMBUSIO_WEB_SERVER_PORT
        self.write_dest_port = NIMBUSIO_WEB_WRITER_PORT
        self.known_clusters = dict()
        self.known_collections = LRUCache(COLLECTION_CACHE_SIZE) 
        self.management_api_request_dest_hosts = \
            deque(NIMBUSIO_MANAGEMENT_API_REQUEST_DEST.strip().split())
        self.request_counter = 0

    def init(self):
        #import logging
        #import traceback
        #from tools.database_connection import get_central_connection
        log = logging.getLogger("init")
        log.info("init start")
        self.conn = retry_central_connection(
            isolation_level=psycopg2.extensions.ISOLATION_LEVEL_AUTOCOMMIT)
        log.info("init complete")

        self.redis = StrictRedis(host = REDIS_HOST,
                                 port = REDIS_PORT,
                                 db = REDIS_DB)

        self.init_complete.set()

    def _parse_collection(self, hostname):
        "return the Nimbus.io collection name from host name"
        offset = -1 * ( len(self.service_domain) + 1 )
        return hostname[:offset]

    def _hosts_for_collection(self, collection):
        "return a list of hosts for this collection"
        cluster_id = self._cluster_for_collection(collection)
        if cluster_id is None:
            return None
        cluster_info = self._cluster_info(cluster_id)
        return cluster_info['hosts']

    @_supervise_db_interaction
    def _db_cluster_for_collection(self, collection):
        # FIXME how do we handle null result here? do we just cache the null
        # result?
        row = self.conn.fetch_one_row(
            "select cluster_id from nimbusio_central.collection where name=%s",
            [collection, ])
        if row:
            return row[0]

    def _cluster_for_collection(self, collection, _retries=0):
        "return cluster ID for collection"
        if collection in self.known_collections:
            return self.known_collections[collection]
        result = self._db_cluster_for_collection(collection,
            cache_check_func = 
                lambda: self.known_collections.get(collection, None))
        self.known_collections[collection] = result
        return result
            
    @_supervise_db_interaction
    def _db_cluster_info(self, cluster_id):
        rows = self.conn.fetch_all_rows("""
            select name, hostname, node_number_in_cluster 
            from nimbusio_central.node 
            where cluster_id=%s 
            order by node_number_in_cluster""", 
            [cluster_id, ])
    
        info = dict(rows = list(rows), 
                    hosts = deque([r[1] for r in rows]))

        return info

    def _cluster_info(self, cluster_id):
        "return info about a cluster and its hosts"
        if cluster_id in self.known_clusters:
            return self.known_clusters[cluster_id]
        
        info = self._db_cluster_info(cluster_id, 
            cache_check_func=lambda: self.known_clusters.get(cluster_id, None))
        
        self.known_clusters[cluster_id] = info 
        return info

    def check_availability(self, hosts, dest_port, _resolve_cache=dict()):
        "return set of hosts we think are available" 
        log = logging.getLogger("check_availability")

        available = set()
        if not hosts:
            return available

        addresses = []
        for host in hosts:
            if not host in _resolve_cache:
                _resolve_cache[host] = socket.gethostbyname(host)
            addresses.append(_resolve_cache[host])

        redis_keys = [ REDIS_WEB_MONITOR_HASHKEY_FORMAT % (a, dest_port, )
                       for a in addresses ]

        try:
            redis_values = self.redis.hmget(REDIS_WEB_MONITOR_HASH_NAME,
                                            redis_keys)
        except RedisError as err:
            log.warn("redis error querying availability for %s: %r"
                % ( REDIS_WEB_MONITOR_HASH_NAME, redis_keys, ))
            # just consider everything available. it's the best we can do.
            available.update(hosts)
            return available

        unknown = []
        for idx, val in enumerate(redis_values):
            if val is None:
                unknown.append((hosts[idx], redis_keys[idx], ))
                continue
            try:
                status = json.loads(val)
            except Exception, err:
                log.warn("cannot decode %s %s %s %r" % ( 
                    REDIS_WEB_MONITOR_HASH_NAME, hosts[idx], 
                    redis_keys[idx], val, ))
            else:
                if status["reachable"]:
                    available.add(hosts[idx])
            
        if unknown:
            log.warn("no availability info in redis for hkeys: %s %r" % 
                ( REDIS_WEB_MONITOR_HASH_NAME, unknown, ))
            if len(unknown) == len(hosts):
                available.update(hosts)

        return available

    @staticmethod
    def _reject(code, reason=None):
        "return a go away response"
        log = logging.getLogger("reject")
        http_error_str = httplib.responses.get(code, "unknown")
        log.debug("reject: %d %s %r" % (code, http_error_str, reason, ))
        if reason is None:
            reason = http_error_str
        return { 'close': 'HTTP/1.0 %d %s\r\n\r\n%s' % ( 
                  code, http_error_str, reason, ) }

    def route(self, hostname, method, path, _query_string, start=None):
        """
        route a to a host in the appropriate cluster, using simple round-robin
        among the hosts in a cluster
        """
        log = logging.getLogger("route")

        self.init_complete.wait()

        self.request_counter += 1
        request_num = self.request_counter

        log.debug("request %d: host=%r, method=%r, path=%r, query=%r, start=%r" % 
            (request_num, hostname, method, path, _query_string, start)) 


        # TODO: be able to handle http requests from http 1.0 clients w/o a
        # host header to at least the website, if nothing else.
        if hostname is None or (not hostname.endswith(self.service_domain)):
            return self._reject(httplib.NOT_FOUND)

        if hostname == self.service_domain:
            # this is not a request specific to any particular collection
            # TODO figure out how to route these requests.
            # in production, this might not matter.
            self.management_api_request_dest_hosts.rotate(1)
            target = self.management_api_request_dest_hosts[0]
            log.debug("request %d to backend host %s" %
                (request_num, target, ))
            return dict(remote = target)

        # determine if the request is a read or write
        if method in ('POST', 'DELETE', 'PUT', 'PATCH', ):
            dest_port = self.write_dest_port
        elif method in ('HEAD', 'GET', ):
            dest_port = self.read_dest_port
        else:
            self._reject(httplib.BAD_REQUEST, "Unknown method")

        collection = self._parse_collection(hostname)
        if collection is None:
            self._reject(httplib.NOT_FOUND, "No such collection")

        hosts = self._hosts_for_collection(collection)

        if hosts is None:
            self._reject(httplib.NOT_FOUND, "No such collection")

        availability = self.check_availability(hosts, dest_port)    

        # find an available host
        for _ in xrange(len(hosts)):
            hosts.rotate(1)
            target = hosts[0]
            if target in availability:
                break
        else:
            # we never found an available host
            now = time.time()
            if start is None:
                log.warn("Request %d No available service, waiting..." %
                    (request_num, ))
                start = now
            if now - start > AVAILABILITY_TIMEOUT:
                return self._reject(httplib.SERVICE_UNAVAILABLE, "Retry later")
            gevent.sleep(RETRY_DELAY)
            return self.route(hostname, method, path, _query_string, start)

        log.debug("request %d to backend host %s port %d" %
            (request_num, target, dest_port, ))
        return dict(remote = "%s:%d" % (target, dest_port, ))

        # no hosts currently available (hosts is an empty list, presumably)