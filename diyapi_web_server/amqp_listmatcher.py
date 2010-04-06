# -*- coding: utf-8 -*-
"""
amqp_listmatcher.py

A class that performs a listmatch query on a random node.
"""
import uuid
import random

import gevent

from messages.database_listmatch import DatabaseListMatch


class AMQPListmatcher(object):
    """Performs a listmatch query on a random node."""

    def __init__(self, amqp_handler, exchanges):
        self.amqp_handler = amqp_handler
        self.exchanges = exchanges

    def _random_exchange(self):
        return random.choice(self.exchanges)

    def listmatch(self, avatar_id, prefix, timeout=None):
        request_id = uuid.uuid1().hex
        message = DatabaseListMatch(
            request_id,
            avatar_id,
            self.amqp_handler.exchange,
            self.amqp_handler.queue_name,
            prefix
        )
        reply_queue = self.amqp_handler.send_message(message,
                                                     self._random_exchange())
        # TODO: select a different node if node is down
        reply = reply_queue.get(timeout=timeout)
        return reply.key_list