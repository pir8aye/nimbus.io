# -*- coding: utf-8 -*-
"""
list_collections_view.py

A View to list collections for a user
"""
import httplib
import json
import logging

import flask

from tools.greenlet_database_util import GetConnection
from tools.data_definitions import http_timestamp_str
from tools.customer_key_lookup import CustomerKeyConnectionLookup
from tools.collection import compute_default_collection_name

from web_collection_manager.connection_pool_view import ConnectionPoolView
from web_collection_manager.authenticator import authenticate

rules = ["/customers/<username>/collections", ]
endpoint = "list_collections"

def _list_collections(connection, customer_id):
    """
    list all collections for the customer, for all clusters
    """
    cursor = connection.cursor()
    cursor.execute("""
        select name, versioning, access_control, creation_time 
        from nimbusio_central.collection   
        where customer_id = %s and deletion_time is null
        """, [customer_id, ])
    result = cursor.fetchall()
    cursor.close()

    return result

class ListCollectionsView(ConnectionPoolView):
    methods = ["GET", ]

    def dispatch_request(self, username):
        log = logging.getLogger("ListCollectionsView")
        log.info("user_name = {0}".format(username))

        with GetConnection(self.connection_pool) as connection:

            customer_key_lookup = \
                CustomerKeyConnectionLookup(self.memcached_client,
                                            connection)
            customer_id = authenticate(customer_key_lookup,
                                       username,
                                       flask.request)
            if customer_id is None:
                flask.abort(httplib.UNAUTHORIZED)

            raw_collection_list = _list_collections(connection, customer_id)

        # ticket #50 When listing collections for a user, show whether a
        # collection is a default collection.
        default_collection_name = compute_default_collection_name(username)

        collection_list = list()
        for raw_entry in raw_collection_list:
            name, versioning, raw_access_control, raw_creation_time = raw_entry
            if raw_access_control is None:
                access_control = None
            else:
                access_control = json.loads(raw_access_control)
            entry = {"name" : name, 
                     "default_collection" : name == default_collection_name,
                     "versioning" : versioning, 
                     "access_control" : access_control,
                     "creation-time" : http_timestamp_str(raw_creation_time)}
            collection_list.append(entry)

        # 2012-08-16 dougfort Ticket #28 - set content_type
        # 2012-08-16 dougfort Ticket #29 - format json for debuging
        return flask.Response(json.dumps(collection_list, 
                                         sort_keys=True, 
                                         indent=4), 
                              status=httplib.OK,
                              content_type="application/json")

view_function = ListCollectionsView.as_view(endpoint)

