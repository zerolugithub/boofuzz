from __future__ import absolute_import

import errno
import math
import socket
import ssl
import struct
import sys

import six
from future.utils import raise_

from . import exception, helpers, ip_constants, itarget_connection

ETH_P_IP = 0x0800  # Ethernet protocol: Internet Protocol packet, see Linux if_ether.h docs for more details.


def _seconds_to_second_microsecond_struct(seconds):
    """Convert floating point seconds value to second/useconds struct used by socket library."""
    microseconds_per_second = 1000000
    whole_seconds = int(math.floor(seconds))
    whole_microseconds = int(math.floor((seconds % 1) * microseconds_per_second))
    return struct.pack('ll', whole_seconds, whole_microseconds)


class SocketConnection(itarget_connection.ITargetConnection):
    """ITargetConnection implementation using sockets.

    Supports UDP, TCP, SSL, raw layer 2 and raw layer 3 packets.

    Examples::

        tcp_connection = SocketConnection(host='127.0.0.1', port=17971)
        udp_connection = SocketConnection(host='127.0.0.1', port=17971, proto='udp')
        udp_connection_2_way = SocketConnection(host='127.0.0.1', port=17971, proto='udp', bind=('127.0.0.1', 17972)
        udp_broadcast = SocketConnection(host='127.0.0.1', port=17971, proto='udp', bind=('127.0.0.1', 17972),
                                         udp_broadcast=True)
        raw_layer_2 = (host='lo', proto='raw-l2')
        raw_layer_2 = (host='lo', proto='raw-l2',
                       l2_dst='\\xFF\\xFF\\xFF\\xFF\\xFF\\xFF', ethernet_proto=socket_connection.ETH_P_IP)
        raw_layer_3 = (host='lo', proto='raw-l3')


    Args:
        host (str): Hostname or IP address of target system, or network interface string if using raw-l2 or raw-l3.
        port (int): Port of target service. Required for proto values 'tcp', 'udp', 'ssl'.
        proto (str): Communication protocol ("tcp", "udp", "ssl", "raw-l2", "raw-l3"). Default "tcp".
            raw-l2: Send packets at layer 2. Must include link layer header (e.g. Ethernet frame).
            raw-l3: Send packets at layer 3. Must include network protocol header (e.g. IPv4).
        bind (tuple (host, port)): Socket bind address and port. Required if using recv() with 'udp' protocol.
        send_timeout (float): Seconds to wait for send before timing out. Default 5.0.
        recv_timeout (float): Seconds to wait for recv before timing out. Default 5.0.
        ethernet_proto (int): Ethernet protocol when using 'raw-l3'. 16 bit integer. Default ETH_P_IP (0x0800).
            See "if_ether.h" in Linux documentation for more options.
        l2_dst (str): Layer 2 destination address (e.g. MAC address). Used only by 'raw-l3'.
            Default '\xFF\xFF\xFF\xFF\xFF\xFF' (broadcast).
        udp_broadcast (bool): Set to True to enable UDP broadcast. Must supply appropriate broadcast address for send()
            to work, and '' for bind host for recv() to work.
        server (bool): Set to True to enable server side fuzzing.
        keyfile (str): The file to use for the SSL key when server side fuzzing with proto ssl.
        certfile (str): The file to use for the SSL certificate when server side fuzzing with proto ssl.
    """
    _PROTOCOLS = ["tcp", "ssl", "udp", "raw-l2", "raw-l3"]
    _PROTOCOLS_PORT_REQUIRED = ["tcp", "ssl", "udp"]
    MAX_PAYLOADS = {"raw-l2": 1514,
                    "raw-l3": 1500,
                    # UDPv4 theoretical limit; actual value dynamically generated by constructor:
                    "udp": ip_constants.UDP_MAX_PAYLOAD_IPV4_THEORETICAL,
                    }

    def __init__(self,
                 host,
                 port=None,
                 proto="tcp",
                 bind=None,
                 send_timeout=5.0,
                 recv_timeout=5.0,
                 ethernet_proto=ETH_P_IP,
                 l2_dst=b'\xFF' * 6,
                 udp_broadcast=False,
                 server=False,
                 keyfile=None,
                 certfile=None):
        self.MAX_PAYLOADS["udp"] = helpers.get_max_udp_size()

        self.host = host
        self.port = port
        self.bind = bind
        self._recv_timeout = recv_timeout
        self._send_timeout = send_timeout
        self.proto = proto.lower()
        self.ethernet_proto = ethernet_proto
        self.l2_dst = l2_dst
        self._udp_broadcast = udp_broadcast
        self.server = server
        self.keyfile = keyfile
        self.certfile = certfile

        self._sock = None
        self._udp_client_port = None

        if self.proto not in self._PROTOCOLS:
            raise exception.SullyRuntimeError("INVALID PROTOCOL SPECIFIED: %s" % self.proto)

        if self.proto in self._PROTOCOLS_PORT_REQUIRED and self.port is None:
            raise ValueError("__init__() argument port required for protocol {0}".format(self.proto))

    def close(self):
        """
        Close connection to the target.

        Returns:
            None
        """
        self._sock.close()
        if self.server and (self.proto == "tcp" or self.proto == "ssl"):
            self._serverSock.close()

    def open(self):
        """
        Opens connection to the target. Make sure to call close!

        Returns:
            None
        """
        # Create socket
        if self.proto == "tcp" or self.proto == "ssl":
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        elif self.proto == "udp":
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            if self.bind:
                self._sock.bind(self.bind)
            if self._udp_broadcast:
                self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, True)
        elif self.proto == "raw-l2":
            self._sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW)
        elif self.proto == "raw-l3":
            self._sock = socket.socket(socket.AF_PACKET, socket.SOCK_DGRAM)
        else:
            raise exception.SullyRuntimeError("INVALID PROTOCOL SPECIFIED: %s" % self.proto)

        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDTIMEO,
                              _seconds_to_second_microsecond_struct(self._send_timeout))
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVTIMEO,
                              _seconds_to_second_microsecond_struct(self._recv_timeout))

        if self.server:
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._sock.bind((self.host, self.port))

            if self.proto == "tcp" or self.proto == "ssl":
                self._serverSock = self._sock
                self._serverSock.listen(1)
                self._sock, addr = self._serverSock.accept()

        # Connect is needed only for TCP protocols
        elif self.proto == "tcp" or self.proto == "ssl":
            try:
                self._sock.connect((self.host, self.port))
            except socket.error as e:
                if e.errno in [errno.ECONNREFUSED, errno.EINPROGRESS, errno.ETIMEDOUT]:
                    raise exception.BoofuzzTargetConnectionFailedError(str(e))
                else:
                    raise

        # if SSL is requested, then enable it.
        if self.proto == "ssl":
            if self.server:
                ssl_sock = ssl.wrap_socket(self._sock, keyfile=self.keyfile, certfile=self.certfile,
                                                      server_side=True)
                self._sock = ssl_sock
            else:
                ssl_sock = ssl.wrap_socket(self._sock)
                self._sock = ssl_sock

    def recv(self, max_bytes):
        """
        Receive up to max_bytes data from the target.

        Args:
            max_bytes (int): Maximum number of bytes to receive.

        Returns:
            Received data.
        """
        try:
            if self.proto in ['tcp', 'ssl']:
                data = self._sock.recv(max_bytes)
            elif self.proto == 'udp':
                if self.bind or self.server:
                    data, self._udp_client_port = self._sock.recvfrom(max_bytes)
                else:
                    raise exception.SullyRuntimeError(
                        "SocketConnection.recv() for UDP requires a bind address/port."
                        " Current value: {}".format(self.bind))
            elif self.proto in ['raw-l2', 'raw-l3']:
                # receive on raw is not supported. Since there is no specific protocol for raw, we would just have to
                # dump everything off the interface anyway, which is probably not what the user wants.
                data = six.binary_type(b'')
            else:
                raise exception.SullyRuntimeError("INVALID PROTOCOL SPECIFIED: %s" % self.proto)
        except socket.timeout:
            data = six.binary_type(b'')
        except socket.error as e:
            if e.errno == errno.ECONNABORTED:
                raise_(exception.BoofuzzTargetConnectionAborted(socket_errno=e.errno, socket_errmsg=e.strerror),
                       None,
                       sys.exc_info()[2])
            elif (e.errno == errno.ECONNRESET) or \
                    (e.errno == errno.ENETRESET) or \
                    (e.errno == errno.ETIMEDOUT):
                raise_(exception.BoofuzzTargetConnectionReset(), None, sys.exc_info()[2])
            elif e.errno == errno.EWOULDBLOCK:  # timeout condition if using SO_RCVTIMEO or SO_SNDTIMEO
                data = six.binary_type(b'')
            else:
                raise

        return data

    def send(self, data):
        """
        Send data to the target. Only valid after calling open!
        Some protocols will truncate; see self.MAX_PAYLOADS.

        Args:
            data: Data to send.

        Returns:
            int: Number of bytes actually sent.
        """
        try:
            data = data[:self.MAX_PAYLOADS[self.proto]]
        except KeyError:
            pass  # data = data

        try:
            if self.proto == "tcp":
                num_sent = self._sock.send(data)
            elif self.proto == "ssl":
                if len(data) > 0:
                    num_sent = self._sock.send(data)
                else:
                    num_sent = 0
            elif self.proto == "udp":
                if self.server:
                    if self._udp_client_port is None:
                        raise exception.BoofuzzError("recv must be called before send with udp fuzzing servers.")
                    num_sent = self._sock.sendto(data, self._udp_client_port)
                else:
                    num_sent = self._sock.sendto(data, (self.host, self.port))
            elif self.proto == "raw-l2":
                num_sent = self._sock.sendto(data, (self.host, 0))
            elif self.proto == "raw-l3":
                # Address tuple: (interface string,
                #                 Ethernet protocol number,
                #                 packet type (recv only),
                #                 hatype (recv only),
                #                 Ethernet address)
                # See man 7 packet for more details.
                num_sent = self._sock.sendto(data, (self.host, self.ethernet_proto, 0, 0, self.l2_dst))
            else:
                raise exception.SullyRuntimeError("INVALID PROTOCOL SPECIFIED: %s" % self.proto)
        except socket.error as e:
            if e.errno == errno.ECONNABORTED:
                raise_(exception.BoofuzzTargetConnectionAborted(socket_errno=e.errno, socket_errmsg=e.strerror),
                       None, sys.exc_info()[2])
            elif (e.errno == errno.ECONNRESET) or \
                    (e.errno == errno.ENETRESET) or \
                    (e.errno == errno.ETIMEDOUT) or \
                    (e.errno == errno.EPIPE):
                raise_(exception.BoofuzzTargetConnectionReset(), None, sys.exc_info()[2])
            else:
                raise
        return num_sent

    @property
    def info(self):
        return '{0}:{1}'.format(self.host, self.port)
