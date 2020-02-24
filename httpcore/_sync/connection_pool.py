from ssl import SSLContext
from typing import Callable, Dict, List, Optional, Set, Tuple, Union

from .._threadlock import ThreadLock
from .base import SyncByteStream, SyncHTTPTransport, ConnectionState
from .http2 import SyncHTTP2Connection
from .http11 import SyncHTTP11Connection

Origin = Tuple[bytes, bytes, int]
URL = Tuple[bytes, bytes, int, bytes]
Headers = List[Tuple[bytes, bytes]]
TimeoutDict = Dict[str, Optional[float]]


class ResponseByteStream(SyncByteStream):
    def __init__(
        self,
        stream: SyncByteStream,
        connection: Union[SyncHTTP11Connection, SyncHTTP2Connection],
        callback: Callable,
    ) -> None:
        """
        A wrapper around the response stream that we return from `.request()`.

        Ensures that when `stream.close()` is called, the connection pool
        is notified via a callback.
        """
        self.stream = stream
        self.connection = connection
        self.callback = callback

    def __iter__(self):
        for chunk in self.stream:
            yield chunk

    def close(self):
        try:
            #  Call the underlying stream close callback.
            # This will be a call to `SyncHTTP11Connection._response_closed()``.
            self.stream.close()
        finally:
            #  Call the connection pool close callback.
            # This will be a call to `SyncConnectionPool._response_closed()``.
            self.callback(self.connection)


class SyncConnectionPool(SyncHTTPTransport):
    """
    A connection pool for making HTTP requests.

    **Parameters:**

    * **ssl_context** - `Optional[SSLContext]` - An SSL context to use for verifying connections.
    """

    def __init__(self, ssl_context: SSLContext = None, http2: bool = False):
        self.ssl_context = SSLContext() if ssl_context is None else ssl_context
        self.http2 = http2
        self.connections: Dict[
            Origin, Set[Union[SyncHTTP11Connection, SyncHTTP2Connection]]
        ] = {}
        self.thread_lock = ThreadLock()

    def request(
        self,
        method: bytes,
        url: URL,
        headers: Headers = None,
        stream: SyncByteStream = None,
        timeout: TimeoutDict = None,
    ) -> Tuple[bytes, int, bytes, Headers, SyncByteStream]:
        origin = url[:3]
        connection = self._get_connection_from_pool(origin)

        if connection is None:
            if self.http2:
                connection = SyncHTTP2Connection(
                    origin=origin, ssl_context=self.ssl_context,
                )
            else:
                connection = SyncHTTP11Connection(
                    origin=origin, ssl_context=self.ssl_context,
                )
            with self.thread_lock:
                self.connections.setdefault(origin, set())
                self.connections[origin].add(connection)

        response = connection.request(
            method, url, headers=headers, stream=stream, timeout=timeout
        )
        wrapped_stream = ResponseByteStream(
            response[4], connection=connection, callback=self._response_closed
        )
        return response[0], response[1], response[2], response[3], wrapped_stream

    def _get_connection_from_pool(
        self, origin: Origin
    ) -> Optional[Union[SyncHTTP11Connection, SyncHTTP2Connection]]:
        # Determine expired keep alive connections on this origin.
        reuse_connection = None
        connections_to_close = set()

        with self.thread_lock:
            if origin in self.connections:
                connections = self.connections[origin]
                for connection in list(connections):
                    if connection.state == ConnectionState.IDLE:
                        if connection.is_connection_dropped():
                            # IDLE connections that have been dropped should be
                            # removed from the pool.
                            connections_to_close.add(connection)
                            connections.remove(connection)
                        else:
                            # IDLE connections that are still maintained may
                            # be reused.
                            reuse_connection = connection

                # Clean up the connections mapping if we've no connections
                # remaining for this origin.
                if not connections:
                    del self.connections[origin]

            #  Mark the connection as ACTIVE before we return it, so that it
            # will not be re-acquired.
            if reuse_connection is not None:
                reuse_connection.state = ConnectionState.ACTIVE

        # Close any dropped connections.
        for connection in connections_to_close:
            connection.close()

        return reuse_connection

    def _response_closed(
        self, connection: Union[SyncHTTP11Connection, SyncHTTP2Connection]
    ):
        with self.thread_lock:
            if connection.state == ConnectionState.CLOSED:
                self.connections[connection.origin].remove(connection)
                if not self.connections[connection.origin]:
                    del self.connections[connection.origin]

    def close(self) -> None:
        connections_to_close = set()

        with self.thread_lock:
            for connection_set in self.connections.values():
                connections_to_close.update(connection_set)
            self.connections.clear()

        # Close all connections
        for connection in connections_to_close:
            connection.close()