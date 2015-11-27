#!/usr/bin/env python3

import asyncio
import os
import sys
import logging
import struct
import hashlib
import argparse
import aiodns
from concurrent.futures import FIRST_COMPLETED, CancelledError
from logging import StreamHandler
from socket import inet_aton, inet_ntoa
from random import randint
from binascii import hexlify, a2b_hex
from bencodepy import encode as bencode, decode as bdecode, DecodingError

HANDSHAKE = 1
MESSAGE_LEN = 2
MESSAGE_TYPE = 3
MESSAGE_PAYLOAD = 4

class SetQueue(asyncio.Queue):
    def _init(self, maxsize):
        self._queue = set()
    def _put(self, item):
        self._queue.add(item)
    def _get(self):
        return self._queue.pop()

TIMEOUT = 5
RETRIES = 2

resolver = None
nodeid = None
nodes = SetQueue()
values = SetQueue()
all_peers = set()
metadata_size = 0
metadata = set()
full_metadata = b''

keep_running = False
get_peers_in_progress = 0
get_metadatas_in_progress = 0

class BitTorrentProtocol:
    def __init__(self, infohash, peerid):
        self.handshake_complete = asyncio.Event()
        self.extended_handshake_complete = asyncio.Event()
        self.metadata_block_received = asyncio.Event()
        self.error = asyncio.Event()

        self.infohash = infohash
        self.peerid = peerid

        self.state = HANDSHAKE
        self.field_len = 68
        self.field = b''
        self.leftover = b''
        self.metadata_size = 0
        self.metadata_block = b''

    def connection_made(self, transport):
        self.transport = transport
        self.transport.write(
            b'\x13BitTorrent protocol'
            b'\x00\x00\x00\x00\x00\x10\x00\x04' + \
            self.infohash + self.peerid
        )

    def data_received(self, data):
        data = self.leftover + data

        if len(self.field) + len(data) < self.field_len:
            self.field += data
            self.leftover = b''
        elif len(self.field) + len(data) == self.field_len:
            self.field += data
            self.leftover = b''
            self.parse_field()
        else:
            n = self.field_len - len(self.field)
            self.field += data[:n]
            self.leftover = data[n:]
            self.parse_field()

            if len(self.leftover) >= self.field_len and not self.error.is_set():
                self.data_received(b'')

    def eof_received(self):
        logger.debug('EOF received.')
        self.error.set()

    def connection_lost(self, exc):
        logger.debug('Connection lost: {}'.format(exc))
        self.error.set()

    def parse_field(self):
        if self.state == HANDSHAKE:
            if not self.field[:20] == b'\x13BitTorrent protocol':
                logger.debug('Invalid pstr.')
                self.error.set()
                return

            if int.from_bytes(self.field[20:28], byteorder='big') & 0x0000000000100000 == 0:
                logger.debug('Peer does not support extension protocol.')
                self.error.set()
                return

            if int.from_bytes(self.field[20:28], byteorder='big') & 0x0000000000000004 == 0:
                logger.debug('Peer does not support fast protocol.')
                self.error.set()
                return

            self.state = MESSAGE_LEN
            self.field_len = 4
            self.handshake_complete.set()

            extended_handshake = bencode({
                'm': {b'ut_metadata': 2},
                'v': 'S.P.E.W.'
            })
            self.write_extended_message(0, extended_handshake)
            logger.debug('Sent extended handshake.')
        elif self.state == MESSAGE_LEN:
            self.message_len = int.from_bytes(self.field, byteorder='big')
            if self.message_len == 0:
                self.state = MESSAGE_LEN
                self.field = 4
            else:
                self.state = MESSAGE_TYPE
                self.field_len = 1
        elif self.state == MESSAGE_TYPE:
            self.message_type = int.from_bytes(self.field, byteorder='big')
            if self.message_len == 1:
                self.state = MESSAGE_LEN
                self.field = 4
            else:
                self.message_len -= 1
                self.field_len = self.message_len
                self.state = MESSAGE_PAYLOAD
        elif self.state == MESSAGE_PAYLOAD:
            self.parse_message()
            self.field_len = 4
            self.state = MESSAGE_LEN
        else:
            logger.error('Invalid state.')
            self.error.set()

        self.field = b''

    def parse_message(self):
        logger.debug({
            0: 'CHOKE',
            1: 'UNCHOKE',
            2: 'INTERESTED',
            3: 'NOT INTERESTED',
            4: 'HAVE',
            5: 'BITFIELD',
            6: 'REQUEST',
            7: 'PIECE',
            8: 'CANCEL',
            9: 'PORT',
            13: 'SUGGEST PIECE',
            14: 'HAVE ALL',
            15: 'HAVE NONE',
            16: 'REJECT REQUEST',
            17: 'ALLOWED FAST',
            20: 'EXTENDED'
        }.get(self.message_type, 'UNKNOWN MESSAGE'))
        if self.message_type == 20:
            self.parse_extended_message()

    def parse_extended_message(self):
        extended_message_type = self.field[0]
        message = self.field[1:]

        if extended_message_type == 0:
            try:
                message = bdecode(message)
            except DecodingError:
                self.error.set()
                return

            if b'm' not in message:
                logger.debug('"m" not in extended handshake.')
                self.error.set()
                return

            self.extended_message_types = message[b'm']

            if b'ut_metadata' not in self.extended_message_types:
                logger.debug('Peer does not support metadata protocol.')
                self.error.set()
                return

            if b'metadata_size' not in message:
                logger.debug('Peer did not send "metadata_size" in extended handshake.')
                self.error.set()
                return

            self.metadata_size = message[b'metadata_size']
            logger.info('metadata size: {}'.format(self.metadata_size))
            self.extended_handshake_complete.set()

            self.write_message(15, b'') # have none
            logger.debug('Sent HAVE NONE.')
            self.write_message(0, b'') # choke
            logger.debug('Sent CHOKE.')
            self.write_message(3, b'') # not interesete
            logger.debug('Sent NOT INTERESTED.')
        elif extended_message_type == self.extended_message_types[b'ut_metadata']:
            original_message = message
            try:
                message = bdecode(message)
            except DecodingError:
                self.error.set()
                return

            if message[b'msg_type'] == 0:
                reply = {
                    'msg_type': 2,
                    'piece': message[b'piece']
                }
            elif message[b'msg_type'] == 2:
                logger.debug('Request for metadata rejected.')
                return
            elif message[b'msg_type'] == 1:
                size = len(original_message) - len(bencode(message))
                logger.debug('Got a metadata block of size: {}'.format(size))

                self.metadata_block = original_message[-size:]
                self.metadata_block_received.set()

    def get_metadata_block(self, n):
        message = {
            'msg_type': 0,
            'piece': n
        }
        logger.info('Requesting piece {} of metadata.'.format(n))
        msg = bencode(message)
        self.write_extended_message(self.extended_message_types[b'ut_metadata'], msg)

    def write_message(self, type, msg):
        msg_len = 1 + len(msg)
        self.transport.write(msg_len.to_bytes(length=4, byteorder='big') + \
                             bytes([type]) + msg)

    def write_extended_message(self, ex_type, msg):
        self.write_message(20, bytes([ex_type]) + msg)

class DhtProtocol:
    def __init__(self, query_type, nodeid,
                 target=None,
                 infohash=None,
                 implied_port=None,
                 port=None,
                 token=None):
        self.query_type = query_type
        self.nodeid = nodeid
        self.target = target
        self.infohash = infohash
        self.implied_port = implied_port
        self.port = port
        self.token = token

        self.tid = struct.pack('!H', randint(0, 65535))
        self.reply_received = asyncio.Event(loop=loop)

    def construct_message(self):
        args = {
            'ping': {
                'id': self.nodeid
            },
            'find_node': {
                'id': self.nodeid,
                'target': self.target
            },
            'get_peers': {
                'id': self.nodeid,
                'info_hash': self.infohash
            },
            'announce_peer': {
                'id': self.nodeid,
                'implied_port': self.implied_port,
                'info_hash': self.infohash,
                'port': self.port,
                'token': self.token
            }
        }.get(self.query_type, None)

        if args == None:
            raise RuntimeError('Invalid DHT query type: {}'.format(
                self.query_type))

        return bencode({
            't': self.tid,
            'y': 'q',
            'q': self.query_type,
            'a': args
        })

    def connection_made(self, transport):
        self.transport = transport
        self.send_message()

    def send_message(self):
        message = self.construct_message()
        logger.debug('Sending DHT query.')
        self.transport.sendto(message)

    def datagram_received(self, data, addr):
        try:
            message = bdecode(data)
        except DecodingError:
            logger.debug('Received invalid bencoding in reply. Discarded.')
            return

        if b't' not in message:
            logger.debug('Received invalid reply. Discarded')
            return

        if message[b't'] != self.tid:
            logger.debug('Received reply with invalid transaction ID. Discarded.')
            return

        if b'r' not in message or b'id' not in message[b'r']:
            logger.debug('Received invalid reply. Discarded.')
            return

        logger.debug('Received DHT reply from {}:{} with node ID {}.'.format(
            addr[0], addr[1], hexlify(message[b'r'][b'id']).decode()))

        self.reply = message[b'r']
        self.reply_received.set()

    def error_received(self, exc):
        pass

    def connection_lost(self, exc):
        pass

    def retry(self):
        logger.debug('Retrying...')
        self.send_message()

@asyncio.coroutine
def ping(loop, host, port):
    try:
        transport, protocol = yield from loop.create_datagram_endpoint(
            lambda: DhtProtocol('ping', nodeid=nodeid),
            remote_addr=(host, port))
    except OSError as e:
        logger.debug('Error opening socket for "ping": {}'.format(e))

    for i in range(RETRIES):
        try:
            yield from asyncio.wait_for(
                protocol.reply_received.wait(),
                timeout=TIMEOUT)
        except asyncio.TimeoutError:
            protocol.retry()
        else:
            break

    transport.close()
    if protocol.reply_received.is_set():
        logger.debug('Reply:', protocol.reply)
        logger.debug('Done.')
    else:
        logger.debug('No reply received.')
    return protocol.reply_received.is_set()

@asyncio.coroutine
def get_peers(loop, host, port, infohash):
    global get_peers_in_progress
    get_peers_in_progress += 1

    try:
        try:
            transport, protocol = yield from loop.create_datagram_endpoint(
                lambda: DhtProtocol('get_peers', nodeid=nodeid, infohash=infohash),
                remote_addr=(host, port))
        except OSError as e:
            logger.debug('Error opening socket for get_peers: {}'.format(e))
            return

        for i in range(RETRIES):
            try:
                yield from asyncio.wait_for(
                    protocol.reply_received.wait(),
                    timeout=5)
            except asyncio.TimeoutError:
                protocol.retry()
            else:
                break

        transport.close()
        if protocol.reply_received.is_set():
            logger.debug('get_peers: done.')
        else:
            logger.debug('get_peers: No reply received.')
            return

        if b'values' in protocol.reply:
            peers = protocol.reply[b'values']
            for p in peers:
                if len(p) != 6:
                    logger.debug('Invalid peer "{}". Ignored.'.format(repr(p)))
                else:
                    all_peers.add(p)
                    yield from values.put(p)
        elif b'nodes' in protocol.reply:
            peers = protocol.reply[b'nodes']
            peers = [peers[i:i+26] for i in range(0, len(peers), 26)]
            for p in peers:
                yield from nodes.put(p[20:])
    finally:
        get_peers_in_progress -= 1

@asyncio.coroutine
def dns_resolve(loop, name):
    logger.info('Resolving: {}'.format(name))
    try:
        result = yield from resolver.query(name, 'A')
    except aiodns.error.DNSError as e:
        raise RuntimeError('Could not resolve name:', name)

    return result[0].host

@asyncio.coroutine
def get_metadata(loop, host, port, infohash):
    global metadata, metadata_size, keep_running, full_metadata, get_metadatas_in_progress

    if not keep_running:
        return

    get_metadatas_in_progress += 1

    try:
        logger.info('Getting metadata from: {}:{}'.format(host, port))

        try:
            transport, protocol = yield from loop.create_connection(
                lambda: BitTorrentProtocol(infohash, nodeid), host, port)
        except OSError as e:
            logger.debug('Connection error: {}'.format(e))
            return

        logger.debug('Connected to peer: {}:{}'.format(host, port))

        done, pending = yield from asyncio.wait(
            [protocol.handshake_complete.wait(),
             protocol.error.wait()],
            return_when=FIRST_COMPLETED,
            timeout=TIMEOUT)

        for task in pending:
            task.cancel()

        if not done or protocol.error.is_set():
            logger.debug('Error communicating with the peer while waiting for the handshake.')
            transport.close()
            return

        done, pending = yield from asyncio.wait(
            [protocol.extended_handshake_complete.wait(),
             protocol.error.wait()],
            return_when=FIRST_COMPLETED,
            timeout=TIMEOUT)

        for task in pending:
            task.cancel()

        if not done or protocol.error.is_set():
            logger.debug('Error communicating with the peer while waiting for the extended handshake.')
            transport.close()
            return

        if metadata_size > 0 and metadata_size != protocol.metadata_size:
            logger.warning('Inconsistent metadata size received.')

        metadata_size = protocol.metadata_size
        metadata_nblocks = int(metadata_size / (16 * 1024))
        metadata_nblocks += 0 if metadata_size % (16 * 1024) == 0 else 1

        while keep_running:
            protocol.metadata_block_received.clear()

            try:
                i = next(i for i in range(metadata_nblocks)
                         if i not in [m[0] for m in metadata])
            except StopIteration as e:
                transport.close()
                return

            protocol.get_metadata_block(i)

            done, pending = yield from asyncio.wait(
                [protocol.metadata_block_received.wait(),
                 protocol.error.wait()],
                return_when=FIRST_COMPLETED,
                timeout=TIMEOUT)

            for task in pending:
                task.cancel()

            if not done or protocol.error.is_set():
                logger.debug('Error communicating with the peer while waiting for metadata block.')
                transport.close()
                return

            metadata.add((i, protocol.metadata_block))

            if {m[0] for m in metadata} == set(range(metadata_nblocks)):
                # metadata complete. hash check.
                m = hashlib.sha1()
                full_metadata = b''
                for i, b in sorted(metadata, key=lambda m: m[0]):
                    full_metadata += b
                    m.update(b)
                if m.digest() != infohash:
                    logger.debug('Invalid metadata received. Hash does not checkout. Discarding.')
                    metadata_size = 0
                    metadata = set()
                    return

                logger.info('Metadata received.')
                full_metadata = bdecode(full_metadata)
                keep_running = False
    finally:
        get_metadatas_in_progress  -= 1

def get_closest_nodes(k, infohash):
    def distance(i, ih):
        byte_distance = lambda x, y: bin(x ^ y).count('1')
        return sum(byte_distance(b1, b2) for b1, b2 in zip(ih, i))
    return sorted(all_peers, key=lambda x: distance(x, infohash))[:k]

@asyncio.coroutine
def main(loop, infohash, filename):
    global keep_running

    # Use router.bittorrent.com as the bootstrapping node.
    logger.info('Using router.bittorrent.com as the bootstrapping node.')
    ip = yield from dns_resolve(loop, 'router.bittorrent.com')
    logger.info('Resolved to: {}'.format(ip))
    yield from nodes.put(inet_aton(ip) + struct.pack('!H', 6881))

    # Recursively search for peers.
    keep_running = True
    while keep_running:
        if values.qsize() > 0:
            while values.qsize() > 0:
                peer = yield from values.get()
                host, port = inet_ntoa(peer[:4]), struct.unpack('!H', peer[4:])[0]
                loop.create_task(get_metadata(loop, host, port, infohash))
        elif get_peers_in_progress < 100 and get_metadatas_in_progress < 100 and nodes.qsize() > 0:
            peer = yield from nodes.get()
            host, port = inet_ntoa(peer[:4]), struct.unpack('!H', peer[4:])[0]
            loop.create_task(get_peers(loop, host, port, infohash))
        else:
            yield

    k = 8
    n = get_closest_nodes(k, infohash)
    n = [[inet_ntoa(p[:4]), struct.unpack('!H', p[4:])[0]]
         for p in n]
    torrent = {
        'nodes': n,
        'info': full_metadata
    }
    with open(filename, 'wb') as f:
        f.write(bencode(torrent))

if __name__ == '__main__':
    nodeid = os.urandom(20)

    parser = argparse.ArgumentParser(description='Convert an infohash into a trackerless torrent file.')

    parser.add_argument('infohash', type=str,
                        help='The infohash of the torrent.')
    parser.add_argument('--file', '-f', type=str,
                        help='The name of the output torrent file. Defaults '
                        'to the infohash with a .torrent extension.')

    args = parser.parse_args()

    args.infohash = a2b_hex(args.infohash)

    if args.file == '':
        args.file = args.infohash + '.torrent'

    logger = logging.getLogger('ih2torrent')
    handler = StreamHandler(sys.stdout)
    handler.setLevel(logging.DEBUG)
    formatter = logging.Formatter('[%(asctime)s][%(levelname)s] %(message)s')
    handler.setFormatter(formatter)
    logger.setLevel(logging.DEBUG)
    logger.addHandler(handler)

    try:
        loop = asyncio.get_event_loop()
        resolver = aiodns.DNSResolver(loop=loop)
        loop.run_until_complete(main(loop, args.infohash, args.file))
    except KeyboardInterrupt:
        print()
        print('Letting the remaining tasks finish before termination.')
    except Exception as e:
        print('Unexpected error:', e)

    pending = asyncio.Task.all_tasks()
    for task in pending:
        task.cancel()
    try:
        loop.run_until_complete(asyncio.gather(*pending))
    except CancelledError:
        pass

    loop.close()