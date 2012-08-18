# -*- coding: utf-8 -*-
"""
Receives HTTP requests and distributes data to backend processes using zeromq

The web server uses gevent instead of the time queue event loop, so it has
some special modules to use gevent.

The web server has a GreenletResilientClient for each data writer

The resilient clients use Deliverator to deliver their messages.
"""
from gevent import monkey
# you must use the latest gevent and have c-ares installed for this to work
# with /etc/hosts 
# hg clone https://bitbucket.org/denis/gevent
monkey.patch_all()

import gevent_zeromq
gevent_zeromq.monkey_patch()

import gevent_psycopg2
gevent_psycopg2.monkey_patch()

import logging
import os
import os.path
import pickle
import signal
import sys

from gevent.pywsgi import WSGIServer
from gevent.event import Event
from gevent_zeromq import zmq
import gevent

from tools.standard_logging import initialize_logging
from tools.greenlet_dealer_client import GreenletDealerClient
from tools.greenlet_resilient_client import GreenletResilientClient
from tools.greenlet_pull_server import GreenletPULLServer
from tools.deliverator import Deliverator
from tools.greenlet_push_client import GreenletPUSHClient
from tools.database_connection import get_central_connection, \
        get_node_local_connection
from tools.event_push_client import EventPushClient
from tools.unified_id_factory import UnifiedIDFactory
from tools.id_translator import InternalIDTranslator
from tools.data_definitions import create_timestamp

from web_server.sql_authenticator import SqlAuthenticator
from web_server.central_database_util import get_cluster_row, get_node_rows
from web_server.space_accounting_client import SpaceAccountingClient

from web_writer.application import Application
from web_writer.watcher import Watcher

_log_path = "%s/nimbusio_web_writer_%s.log" % (
    os.environ["NIMBUSIO_LOG_DIR"], os.environ["NIMBUSIO_NODE_NAME"], )

_node_names = os.environ['NIMBUSIO_NODE_NAME_SEQ'].split()
_local_node_name = os.environ["NIMBUSIO_NODE_NAME"]
_client_tag = "web-writer-%s" % (_local_node_name, )
_web_writer_pipeliner_address = \
    os.environ["NIMBUSIO_WEB_WRITER_PIPELINE_ADDRESS"]
_data_writer_addresses = \
    os.environ["NIMBUSIO_DATA_WRITER_ADDRESSES"].split()
_space_accounting_server_address = \
    os.environ["NIMBUSIO_SPACE_ACCOUNTING_SERVER_ADDRESS"]
_space_accounting_pipeline_address = \
    os.environ["NIMBUSIO_SPACE_ACCOUNTING_PIPELINE_ADDRESS"]
_web_writer_host = os.environ.get("NIMBUSIO_WEB_WRITER_HOST", "")
_web_writer_port = int(os.environ["NIMBUSIO_WEB_WRITER_PORT"])
_wsgi_backlog = int(os.environ.get("NIMBUS_IO_WSGI_BACKLOG", "1024"))
_stats = {
    "archives"    : 0,
}
_repository_path = os.environ["NIMBUSIO_REPOSITORY_PATH"]

def _signal_handler_closure(halt_event):
    def _signal_handler(*_args):
        halt_event.set()
    return _signal_handler

def _get_shard_id(central_connection, cluster_id):
    """
    use node_id as shard id
    """
    for node_row in get_node_rows(central_connection, cluster_id):
        if node_row.name == _local_node_name:
            return node_row.id

    # if we make it here, this cluster is misconfigured
    raise ValueError(
        "node name {0} is not in node rows for cluster {1}".format(
            _local_node_name, cluster_id
        )
    )

class WebWriter(object):
    def __init__(self):
        self._log = logging.getLogger("WebWriter")
        authenticator = SqlAuthenticator()

        self._central_connection = get_central_connection()

        # Ticket #25: must run database operation in a greenlet
        greenlet =  gevent.Greenlet.spawn(get_cluster_row, 
                                           self._central_connection)
        greenlet.join()
        self._cluster_row = greenlet.get()

        greenlet =  gevent.Greenlet.spawn(_get_shard_id,
                                          self._central_connection, 
                                          self._cluster_row.id)

        # Ticket #25: must run database operation in a greenlet
        greenlet.join()
        shard_id = greenlet.get()
        self._unified_id_factory = UnifiedIDFactory(shard_id)

        self._node_local_connection = get_node_local_connection()
        self._deliverator = Deliverator()

        self._zeromq_context = zmq.Context()

        self._pull_server = GreenletPULLServer(
            self._zeromq_context, 
            _web_writer_pipeliner_address,
            self._deliverator
        )
        self._pull_server.link_exception(self._unhandled_greenlet_exception)

        self._data_writer_clients = list()
        for node_name, address in zip(_node_names, _data_writer_addresses):
            resilient_client = GreenletResilientClient(
                self._zeromq_context, 
                node_name,
                address,
                _client_tag,
                _web_writer_pipeliner_address,
                self._deliverator,
                connect_messages=[]
            )
            resilient_client.link_exception(self._unhandled_greenlet_exception)
            self._data_writer_clients.append(resilient_client)

        self._space_accounting_dealer_client = GreenletDealerClient(
            self._zeromq_context, 
            _local_node_name, 
            _space_accounting_server_address
        )
        self._space_accounting_dealer_client.link_exception(
            self._unhandled_greenlet_exception
        )

        push_client = GreenletPUSHClient(
            self._zeromq_context, 
            _local_node_name, 
            _space_accounting_pipeline_address,
        )

        self._accounting_client = SpaceAccountingClient(
            _local_node_name,
            self._space_accounting_dealer_client,
            push_client
        )

        self._event_push_client = EventPushClient(
            self._zeromq_context,
            "web-server"
        )

        # message sent to data writers telling them the server
        # is (re)starting, thereby invalidating any archives
        # that are in progress for this node
        unified_id = self._unified_id_factory.next()
        timestamp = create_timestamp()
        self._event_push_client.info("web-writer-start",
                                     "web writer (re)start",
                                     unified_id=unified_id,
                                     timestamp_repr=repr(timestamp),
                                     source_node_name=_local_node_name)

        self._watcher = Watcher(
            _stats, 
            self._data_writer_clients,
            self._event_push_client
        )

        id_translator_keys_path = os.environ.get(
            "NIMBUS_IO_ID_TRANSLATION_KEYS", 
            os.path.join(_repository_path, "id_translator_keys.pkl"))
        with open(id_translator_keys_path, "r") as input_file:
            id_translator_keys = pickle.load(input_file)

        self._id_translator = InternalIDTranslator(
            id_translator_keys["key"],
            id_translator_keys["hmac_key"], 
            id_translator_keys["iv_key"],
            id_translator_keys["hmac_size"]
        )
        self.application = Application(
            self._central_connection,
            self._node_local_connection,
            self._cluster_row,
            self._unified_id_factory,
            self._id_translator,
            self._data_writer_clients,
            authenticator,
            self._accounting_client,
            self._event_push_client,
            _stats
        )
        self.wsgi_server = WSGIServer((_web_writer_host, _web_writer_port), 
                                      application=self.application,
                                      backlog=_wsgi_backlog
        )

    def start(self):
        self._space_accounting_dealer_client.start()
        self._pull_server.start()
        self._watcher.start()
        for client in self._data_writer_clients:
            client.start()
        self.wsgi_server.start()

    def stop(self):
        self._log.info("stopping wsgi web server")
        self.wsgi_server.stop()
        self._accounting_client.close()
        self._log.debug("killing greenlets")
        self._space_accounting_dealer_client.kill()
        self._pull_server.kill()
        self._watcher.kill()
        for client in self._data_writer_clients:
            client.kill()
        self._log.debug("joining greenlets")
        self._space_accounting_dealer_client.join()
        self._pull_server.join()
        self._watcher.join()
        for client in self._data_writer_clients:
            client.join()
        self._log.debug("closing zmq")
        self._event_push_client.close()
        self._zeromq_context.term()
        self._log.info("closing database connections")
        self._central_connection.close()
        self._node_local_connection.close()

    def _unhandled_greenlet_exception(self, greenlet_object):
        try:
            greenlet_object.get()
        except Exception:
            self._log.exception(str(greenlet_object))
            exctype, value = sys.exc_info()[:2]
            self._event_push_client.exception(
                "unhandled_greenlet_exception",
                str(value),
                exctype=exctype.__name__
            )

def main():
    initialize_logging(_log_path)
    log = logging.getLogger("main")
    halt_event = Event()
    gevent.signal(signal.SIGTERM, _signal_handler_closure(halt_event))

    try:
        web_writer = WebWriter()
        web_writer.start()
    except Exception, instance:
        log.exception(str(instance))
        return -1

    halt_event.wait()
    log.info("halt_event set")

    try:
        web_writer.stop()
    except Exception, instance:
        log.exception(str(instance))
        return -1

    log.info("program terminates normally")
    return 0

if __name__ == '__main__':
    sys.exit(main(*sys.argv[1:]))