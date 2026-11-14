"""Minimal, bounded V2Ray StatsService wire codec; no generated proto dependency."""
from __future__ import annotations

import re


def varint(raw, offset):
    value = 0
    for shift in range(0, 70, 7):
        if offset >= len(raw):
            raise ValueError('truncated_varint')
        byte = raw[offset]
        offset += 1
        value |= (byte & 127) << shift
        if byte < 128:
            return value, offset
    raise ValueError('invalid_varint')


def fields(raw):
    offset = 0
    while offset < len(raw):
        key, offset = varint(raw, offset)
        kind = key & 7
        if kind == 0:
            value, offset = varint(raw, offset)
        elif kind == 2:
            size, offset = varint(raw, offset)
            if size > len(raw)-offset:
                raise ValueError('truncated_message')
            value, offset = raw[offset:offset+size], offset+size
        elif kind in (1, 5):
            size = 8 if kind == 1 else 4
            if offset+size > len(raw):
                raise ValueError('truncated_fixed')
            value, offset = raw[offset:offset+size], offset+size
        else:
            raise ValueError('unsupported_wire_type')
        yield key >> 3, value


def decode_counters(raw):
    result = {}
    for key, message in fields(raw):
        if key != 1 or not isinstance(message, bytes):
            continue
        data = dict(fields(message))
        name = data.get(1, b'').decode('utf-8', errors='strict')
        match = re.fullmatch(r'user>>>g([0-9]+)>>>traffic>>>(uplink|downlink)', name)
        if match:
            value = data.get(2, 0)
            if not isinstance(value, int) or value > 2**60:
                raise ValueError('invalid_counter')
            row = result.setdefault(int(match[1]), {'upload': 0, 'download': 0})
            row['upload' if match[2] == 'uplink' else 'download'] = value
    return result


def query(address='127.0.0.1:19091'):
    import grpc
    with grpc.insecure_channel(address, options=[('grpc.max_receive_message_length', 4*1024*1024)]) as channel:
        call = channel.unary_unary('/v2ray.core.app.stats.command.StatsService/QueryStats',
                                   request_serializer=lambda x: x, response_deserializer=lambda x: x)
        return decode_counters(call(b'', timeout=3))
