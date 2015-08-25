import asyncio
import time
from collections import deque

import tmq.define as td


class Context:
    '''The core handler for tsockets. Does the asyncio loop'''
    def __init__(self, broker, event_loop=None):
        self._broker = broker
        self.tsockets = []
        if event_loop is None:
            event_loop = asyncio.get_event_loop()
        self.event_loop = event_loop
        self.publishers = {}
        self.subscribers = {}
        self.failures = []
        task = self.event_loop.create_task(self._loop())
        self.add_done_callback(task)

    def remove_tsocket(self, tsock):
        self.tsockets.remove(tsock)

    @asyncio.coroutine
    def _loop(self):
        while True:
            start = time.time()
            for s in self.tsockets:
                assert s.context
                self._process_tsocket(s)

            start = time.time() - start
            try:
                yield from asyncio.sleep(td.TMQ_LOOP_TIME - start)
            except ValueError:
                pass

    def _process_tsocket(self, tsock):
        # accept and process connections until they are done
        if tsock.role == td.TMQ_BROKER:
            return self._process_broker(tsock)
        else:
            return self._process_client(tsock)

    def _process_client(self, tsock):
        tasks = []
        while True:
            try:
                conn, addr = tsock.accept()
            except BlockingIOError:
                return tasks
            t = self.event_loop.create_task(
                self._process_client_data(tsock, conn, addr))
            tasks.append(t)

    @asyncio.coroutine
    def _process_client_data(self, tsock, conn, addr):
        try:
            data = yield from get_data(self.event_loop, conn)
        finally:
            conn.close()
        type, pattern, data = td.tmq_unpack(data)
        if type == td.TMQ_SUB:
            # it is data that this socket subscribed to
            tsock.published[pattern].appendleft(data)
        elif type == (td.TMQ_PUB | td.TMQ_CACHE):
            # it is new subscribers to publish to
            if pattern not in tsock.subscribed: raise KeyError
            tsock.subscribed[pattern].update(
                td.tmq_unpack_addresses(data))
        elif type == td.TMQ_PUB | td.TMQ_CACHE | td.TMQ_REMOVE:
            # it is subscribers to remove from publishing to
            if pattern not in tsock.subscribed: raise KeyError
            subscribed = tsock.subscribed[pattern]
            for addr in td.tmq_unpack_addresses(data):
                try: subscribed.remove(addr)
                except KeyError: pass
        else:
            assert 0

    def _process_broker(self, tsock):
        tasks = []
        while True:
            # TODO: process things that need to be sent out
            try:
                conn, addr = tsock.accept()
            except BlockingIOError:
                return tasks
            t = self.event_loop.create_task(
                self._process_broker_data(tsock, conn, addr))
            tasks.append(t)

    @asyncio.coroutine
    def _process_broker_data(self, tsock, conn, addr):
        try:
            data = yield from get_data(self.event_loop, conn)
        finally:
            conn.close()
        type, pattern, data = td.tmq_unpack(data)
        if type == td.TMQ_SUB | td.TMQ_CACHE | td.TMQ_BROKER:
            t = self.event_loop.create_task(
                self._subscriber(tsock, pattern, data))
        elif type == td.TMQ_PUB | td.TMQ_CACHE | td.TMQ_BROKER:
            t = self.event_loop.create_task(
                self._publisher(tsock, pattern, data))
        else:
            raise TypeError
        self.add_done_callback(t)

    @asyncio.coroutine
    def _publisher(self, tsock, pattern, data):
        if pattern not in self.publishers:
            self.publishers[pattern] = set()
        addr = td.tmq_unpack_addresses(data)[0]
        self.publishers[pattern].add(addr)

        # send current subscribers of that token to the new publisher
        addresses = self.subscribers[pattern]
        packet = td.tmq_pack(td.TMQ_PUB | td.TMQ_CACHE, pattern,
                             td.tmq_pack_addresses(addresses))
        s = tsock.socket()
        import ipdb; ipdb.set_trace()
        try:    # TODO: handle failure
            yield from self.event_loop.sock_connect(s, addr)
            yield from self.event_loop.sock_send_all(s, packet)
        finally: s.close()

    @asyncio.coroutine
    def _subscriber(self, tsock, pattern, data):
        addr = td.tmq_unpack_addresses(data)[0]
        if pattern not in self.subscribers:
            self.subscribers[pattern] = {addr}

        if pattern not in self.publishers:
            return  # no publishers for that subscriber (yet)

        packet = td.tmq_pack(td.TMQ_PUB | td.TMQ_CACHE, pattern,
                             td.tmq_pack_address_t(*addr))
        for addr in self.publishers[pattern]:
            s = tsock.socket()
            try:    # TODO: handle failure
                yield from self.event_loop.sock_connect(s, addr)
                yield from self.event_loop.sock_send_all(s, packet)
            finally: s.close()

    def add_done_callback(self, task):
        task.add_done_callback(self._task_callback)

    def _task_callback(self, future):
        '''If a task fails, it is added to failures to be raised
        later'''
        if future.exception():
            self.failures.append(future)



@asyncio.coroutine
def get_data(event_loop, conn):
    '''Get all the data from the socket or raise an error'''
    # TODO: not fully implemented
    data = yield from event_loop.sock_recv(conn, td.TMQ_MSG_LEN)
    return data
