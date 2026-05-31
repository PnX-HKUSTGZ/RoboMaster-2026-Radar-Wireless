#!/usr/bin/env python3
from __future__ import annotations

import json
import random
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Sequence


REF_SOF = 0xA5

CRC8_MODE_LEGACY = "legacy"
CRC8_MODE_REFLECTED = "reflected"
CRC8_MODE_AUTO = "auto"
CRC8_MODE_CHOICES = (CRC8_MODE_LEGACY, CRC8_MODE_REFLECTED, CRC8_MODE_AUTO)
DEFAULT_TX_CRC8_MODE = CRC8_MODE_REFLECTED

AIR_PAYLOAD_LEN = 15
AIR_HEADER = struct.pack(">HH", AIR_PAYLOAD_LEN, AIR_PAYLOAD_LEN)

INFO_ACCESS_CODE = bytes.fromhex("2F6F4C74B914492E")
JAM_ACCESS_CODE = bytes.fromhex("16E8D377151C712D")

AIR_SECTION_ACCESS = "access"
AIR_SECTION_HEADER = "header"
AIR_SECTION_PAYLOAD = "payload"

AIR_BIT_ORDER_OFFICIAL_1404 = "official_1404"
AIR_BIT_ORDER_MSB_ALL = "msb_all"
AIR_BIT_ORDER_LSB_ALL = "lsb_all"
AIR_BIT_ORDER_MSB_ACCESS_HEADER_LSB_PAYLOAD = "msb_access_header_lsb_payload"
AIR_BIT_ORDER_LSB_ACCESS_HEADER_MSB_PAYLOAD = "lsb_access_header_msb_payload"
AIR_BIT_ORDER_AUTO = "auto"
AIR_BIT_ORDER_CHOICES = (
    AIR_BIT_ORDER_OFFICIAL_1404,
    AIR_BIT_ORDER_MSB_ALL,
    AIR_BIT_ORDER_LSB_ALL,
    AIR_BIT_ORDER_MSB_ACCESS_HEADER_LSB_PAYLOAD,
    AIR_BIT_ORDER_LSB_ACCESS_HEADER_MSB_PAYLOAD,
)
AIR_BIT_ORDER_CLI_CHOICES = AIR_BIT_ORDER_CHOICES + (AIR_BIT_ORDER_AUTO,)
DEFAULT_AIR_BIT_ORDER = AIR_BIT_ORDER_MSB_ALL

INFO_SAMPLE_RATE = 1_000_000
INFO_SPS = 52
INFO_BT = 0.35

TEAM_RED = "red"
TEAM_BLUE = "blue"

INFO_FREQUENCY_HZ = {
    TEAM_RED: 433_200_000,
    TEAM_BLUE: 433_920_000,
}

INFO_RF_BANDWIDTH_HZ = 540_000
INFO_SENSITIVITY = 1.5756

JAM_PROFILES = {
    TEAM_RED: {
        1: {"center_freq_hz": 432_200_000, "rf_bandwidth_hz": 940_000, "sensitivity": 2.8323},
        2: {"center_freq_hz": 432_500_000, "rf_bandwidth_hz": 860_000, "sensitivity": 2.5809},
        3: {"center_freq_hz": 432_800_000, "rf_bandwidth_hz": 250_000, "sensitivity": 0.6646},
    },
    TEAM_BLUE: {
        1: {"center_freq_hz": 434_920_000, "rf_bandwidth_hz": 940_000, "sensitivity": 2.8323},
        2: {"center_freq_hz": 434_620_000, "rf_bandwidth_hz": 860_000, "sensitivity": 2.5809},
        3: {"center_freq_hz": 434_320_000, "rf_bandwidth_hz": 250_000, "sensitivity": 0.6646},
    },
}

POSITION_ORDER = ("hero", "engineer", "infantry_3", "infantry_4", "aerial", "sentry")
HP_ORDER = ("hero", "engineer", "infantry_3", "infantry_4", "reserved", "sentry")
BULLET_ORDER = ("hero", "infantry_3", "infantry_4", "aerial", "sentry")
BUFF_ORDER = ("hero", "engineer", "infantry_3", "infantry_4", "sentry")

CMD_INFO_POSITION = 0x0A01
CMD_INFO_HP = 0x0A02
CMD_INFO_BULLET = 0x0A03
CMD_INFO_MACRO = 0x0A04
CMD_INFO_BUFF = 0x0A05
CMD_JAM_KEY = 0x0A06


@dataclass(frozen=True)
class RefereeFrame:
    seq: int
    cmd_id: int
    payload: bytes
    raw: bytes
    crc8_mode: str = CRC8_MODE_AUTO


@dataclass(frozen=True)
class AirPacket:
    access_code: bytes
    header: bytes
    payload: bytes

    @property
    def raw(self) -> bytes:
        return self.access_code + self.header + self.payload


def crc8_dji(data: bytes, init: int = 0xFF) -> int:
    crc = init & 0xFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 0x80:
                crc = ((crc << 1) ^ 0x31) & 0xFF
            else:
                crc = (crc << 1) & 0xFF
    return crc


def _reflect_bits(value: int, width: int) -> int:
    reflected = 0
    for bit_index in range(width):
        reflected = (reflected << 1) | ((value >> bit_index) & 0x01)
    return reflected


def crc8_dji_reflected(data: bytes, init: int = 0xFF) -> int:
    crc = init & 0xFF
    for byte in data:
        crc ^= _reflect_bits(byte, 8)
        for _ in range(8):
            if crc & 0x80:
                crc = ((crc << 1) ^ 0x31) & 0xFF
            else:
                crc = (crc << 1) & 0xFF
    return _reflect_bits(crc, 8)


def crc8_dji_for_mode(data: bytes, mode: str = DEFAULT_TX_CRC8_MODE, init: int = 0xFF) -> int:
    if mode == CRC8_MODE_LEGACY:
        return crc8_dji(data, init=init)
    if mode == CRC8_MODE_REFLECTED:
        return crc8_dji_reflected(data, init=init)
    raise ValueError(f"invalid CRC8 generation mode '{mode}'")


def crc16_dji(data: bytes, init: int = 0xFFFF) -> int:
    crc = init & 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 0x0001:
                crc = (crc >> 1) ^ 0x8408
            else:
                crc >>= 1
    return crc & 0xFFFF


def append_crc8(header_without_crc: bytes, mode: str = DEFAULT_TX_CRC8_MODE) -> bytes:
    if len(header_without_crc) != 4:
        raise ValueError("frame header before CRC8 must be exactly 4 bytes")
    return header_without_crc + bytes([crc8_dji_for_mode(header_without_crc, mode=mode)])


def detect_crc8_mode(header: bytes, mode: str = CRC8_MODE_AUTO) -> str | None:
    if len(header) != 5:
        return None
    if mode not in CRC8_MODE_CHOICES:
        raise ValueError(f"invalid CRC8 verify mode '{mode}'")
    expected = header[-1]
    if mode in (CRC8_MODE_REFLECTED, CRC8_MODE_AUTO) and crc8_dji_reflected(header[:-1]) == expected:
        return CRC8_MODE_REFLECTED
    if mode in (CRC8_MODE_LEGACY, CRC8_MODE_AUTO) and crc8_dji(header[:-1]) == expected:
        return CRC8_MODE_LEGACY
    return None


def verify_crc8(header: bytes, mode: str = CRC8_MODE_AUTO) -> bool:
    return detect_crc8_mode(header, mode=mode) is not None


def verify_crc16(frame: bytes) -> bool:
    if len(frame) < 9:
        return False
    expected = crc16_dji(frame[:-2])
    got = struct.unpack_from("<H", frame, len(frame) - 2)[0]
    return expected == got


def build_referee_frame(cmd_id: int, payload: bytes, seq: int, crc8_mode: str = DEFAULT_TX_CRC8_MODE) -> bytes:
    header = bytearray(5)
    header[0] = REF_SOF
    struct.pack_into("<H", header, 1, len(payload))
    header[3] = seq & 0xFF
    header[4] = crc8_dji_for_mode(header[:4], mode=crc8_mode)

    frame = bytearray(5 + 2 + len(payload) + 2)
    frame[:5] = header
    struct.pack_into("<H", frame, 5, cmd_id)
    frame[7:7 + len(payload)] = payload
    struct.pack_into("<H", frame, len(frame) - 2, crc16_dji(frame[:-2]))
    return bytes(frame)


def parse_referee_frame(frame: bytes, crc8_mode: str = CRC8_MODE_AUTO) -> RefereeFrame:
    if len(frame) < 9:
        raise ValueError("frame too short")
    if frame[0] != REF_SOF:
        raise ValueError("invalid SOF")
    detected_crc8_mode = detect_crc8_mode(frame[:5], mode=crc8_mode)
    if detected_crc8_mode is None:
        raise ValueError("CRC8 mismatch")
    payload_len = struct.unpack_from("<H", frame, 1)[0]
    expected_len = 5 + 2 + payload_len + 2
    if len(frame) != expected_len:
        raise ValueError("unexpected frame length")
    if not verify_crc16(frame):
        raise ValueError("CRC16 mismatch")
    seq = frame[3]
    cmd_id = struct.unpack_from("<H", frame, 5)[0]
    payload = frame[7:-2]
    return RefereeFrame(seq=seq, cmd_id=cmd_id, payload=payload, raw=frame, crc8_mode=detected_crc8_mode)


def iter_referee_frames(byte_stream: bytes, crc8_mode: str = CRC8_MODE_AUTO) -> Iterator[RefereeFrame]:
    cursor = 0
    while cursor + 9 <= len(byte_stream):
        sof_index = byte_stream.find(bytes([REF_SOF]), cursor)
        if sof_index < 0:
            return
        if sof_index + 5 > len(byte_stream):
            return
        payload_len = struct.unpack_from("<H", byte_stream, sof_index + 1)[0]
        frame_len = 5 + 2 + payload_len + 2
        if sof_index + frame_len > len(byte_stream):
            return
        candidate = byte_stream[sof_index:sof_index + frame_len]
        try:
            frame = parse_referee_frame(candidate, crc8_mode=crc8_mode)
        except ValueError:
            cursor = sof_index + 1
            continue
        yield frame
        cursor = sof_index + frame_len


def chunk_bytes(data: bytes, chunk_size: int) -> List[bytes]:
    if len(data) % chunk_size != 0:
        raise ValueError(f"data length {len(data)} is not a multiple of chunk size {chunk_size}")
    return [data[i:i + chunk_size] for i in range(0, len(data), chunk_size)]


def build_air_packets(byte_stream: bytes, access_code: bytes) -> List[AirPacket]:
    payloads = chunk_bytes(byte_stream, AIR_PAYLOAD_LEN)
    return [AirPacket(access_code=access_code, header=AIR_HEADER, payload=payload) for payload in payloads]


def serialize_air_packets(packets: Sequence[AirPacket]) -> bytes:
    return b"".join(packet.raw for packet in packets)


def _validate_air_bit_order(air_bit_order: str) -> str:
    if air_bit_order == AIR_BIT_ORDER_AUTO:
        raise ValueError("auto is only valid for receive-side probing")
    if air_bit_order not in AIR_BIT_ORDER_CHOICES:
        raise ValueError(f"invalid air_bit_order '{air_bit_order}'")
    return air_bit_order


def _bit_direction_for_section(air_bit_order: str, section: str) -> str:
    order = _validate_air_bit_order(air_bit_order)
    if section not in (AIR_SECTION_ACCESS, AIR_SECTION_HEADER, AIR_SECTION_PAYLOAD):
        raise ValueError(f"invalid air packet section '{section}'")

    if order == AIR_BIT_ORDER_MSB_ALL:
        return "msb"
    if order == AIR_BIT_ORDER_LSB_ALL:
        return "lsb"
    if order == AIR_BIT_ORDER_MSB_ACCESS_HEADER_LSB_PAYLOAD:
        return "lsb" if section == AIR_SECTION_PAYLOAD else "msb"
    if order == AIR_BIT_ORDER_LSB_ACCESS_HEADER_MSB_PAYLOAD:
        return "msb" if section == AIR_SECTION_PAYLOAD else "lsb"

    # Conservative #1404 interpretation:
    # Access Code/Header are transmitted low-bit first, while Payload remains
    # the already-packed protocol byte stream. Probe modes remain available for
    # official captures if the terse answer needs a different interpretation.
    if order == AIR_BIT_ORDER_OFFICIAL_1404:
        return "msb" if section == AIR_SECTION_PAYLOAD else "lsb"

    raise ValueError(f"unhandled air_bit_order '{air_bit_order}'")


def bytes_to_bits_msb_first(data: bytes) -> List[int]:
    bits: List[int] = []
    for byte in data:
        for shift in range(7, -1, -1):
            bits.append((byte >> shift) & 0x01)
    return bits


def bytes_to_bits_lsb_first(data: bytes) -> List[int]:
    bits: List[int] = []
    for byte in data:
        for shift in range(0, 8):
            bits.append((byte >> shift) & 0x01)
    return bits


def bytes_to_air_bits(data: bytes, *, air_bit_order: str = DEFAULT_AIR_BIT_ORDER, section: str) -> List[int]:
    direction = _bit_direction_for_section(air_bit_order, section)
    if direction == "lsb":
        return bytes_to_bits_lsb_first(data)
    return bytes_to_bits_msb_first(data)


def bits_to_bytes_msb_first(bits: Sequence[int]) -> bytes:
    if len(bits) % 8 != 0:
        raise ValueError("bit length must be a multiple of 8")
    out = bytearray(len(bits) // 8)
    for i in range(0, len(bits), 8):
        value = 0
        for bit in bits[i:i + 8]:
            value = (value << 1) | (bit & 0x01)
        out[i // 8] = value
    return bytes(out)


def bits_to_bytes_lsb_first(bits: Sequence[int]) -> bytes:
    if len(bits) % 8 != 0:
        raise ValueError("bit length must be a multiple of 8")
    out = bytearray(len(bits) // 8)
    for i in range(0, len(bits), 8):
        value = 0
        for shift, bit in enumerate(bits[i:i + 8]):
            value |= (bit & 0x01) << shift
        out[i // 8] = value
    return bytes(out)


def air_bits_to_bytes(bits: Sequence[int], *, air_bit_order: str = DEFAULT_AIR_BIT_ORDER, section: str) -> bytes:
    direction = _bit_direction_for_section(air_bit_order, section)
    if direction == "lsb":
        return bits_to_bytes_lsb_first(bits)
    return bits_to_bytes_msb_first(bits)


def serialize_air_packet_bits(packet: AirPacket, *, air_bit_order: str = DEFAULT_AIR_BIT_ORDER) -> List[int]:
    return (
        bytes_to_air_bits(packet.access_code, air_bit_order=air_bit_order, section=AIR_SECTION_ACCESS)
        + bytes_to_air_bits(packet.header, air_bit_order=air_bit_order, section=AIR_SECTION_HEADER)
        + bytes_to_air_bits(packet.payload, air_bit_order=air_bit_order, section=AIR_SECTION_PAYLOAD)
    )


def serialize_air_packets_to_bits(
    packets: Sequence[AirPacket],
    *,
    air_bit_order: str = DEFAULT_AIR_BIT_ORDER,
) -> List[int]:
    bits: List[int] = []
    for packet in packets:
        bits.extend(serialize_air_packet_bits(packet, air_bit_order=air_bit_order))
    return bits


def serialize_air_packets_to_bit_bytes(
    packets: Sequence[AirPacket],
    *,
    air_bit_order: str = DEFAULT_AIR_BIT_ORDER,
) -> bytes:
    return bytes(serialize_air_packets_to_bits(packets, air_bit_order=air_bit_order))


def serialized_air_bytes_to_bit_bytes(
    air_bytes: bytes,
    *,
    access_code: bytes,
    air_bit_order: str = DEFAULT_AIR_BIT_ORDER,
) -> bytes:
    packet_len = len(access_code) + 4 + AIR_PAYLOAD_LEN
    if len(air_bytes) % packet_len != 0:
        raise ValueError(f"serialized air byte length {len(air_bytes)} is not a multiple of packet length {packet_len}")
    bits: List[int] = []
    for offset in range(0, len(air_bytes), packet_len):
        packet = AirPacket(
            access_code=air_bytes[offset:offset + len(access_code)],
            header=air_bytes[offset + len(access_code):offset + len(access_code) + 4],
            payload=air_bytes[offset + len(access_code) + 4:offset + packet_len],
        )
        bits.extend(serialize_air_packet_bits(packet, air_bit_order=air_bit_order))
    return bytes(bits)


def recover_air_packets_from_bits(
    bit_items: Sequence[int],
    access_code: bytes,
    *,
    air_bit_order: str = DEFAULT_AIR_BIT_ORDER,
) -> List[AirPacket]:
    access_bits = bytes_to_air_bits(access_code, air_bit_order=air_bit_order, section=AIR_SECTION_ACCESS)
    packet_payload_bits = AIR_PAYLOAD_LEN * 8
    packet_header_bits = 32
    packets: List[AirPacket] = []
    cursor = 0
    max_start = len(bit_items) - (len(access_bits) + packet_header_bits + packet_payload_bits)

    while cursor <= max_start:
        if list(bit_items[cursor:cursor + len(access_bits)]) != access_bits:
            cursor += 1
            continue

        header_start = cursor + len(access_bits)
        header_bits = bit_items[header_start:header_start + packet_header_bits]
        header = air_bits_to_bytes(header_bits, air_bit_order=air_bit_order, section=AIR_SECTION_HEADER)
        length_a, length_b = struct.unpack(">HH", header)
        if length_a != AIR_PAYLOAD_LEN or length_b != AIR_PAYLOAD_LEN:
            cursor += 1
            continue

        payload_start = header_start + packet_header_bits
        payload_bits = bit_items[payload_start:payload_start + packet_payload_bits]
        payload = air_bits_to_bytes(payload_bits, air_bit_order=air_bit_order, section=AIR_SECTION_PAYLOAD)
        packets.append(AirPacket(access_code=access_code, header=header, payload=payload))
        cursor = payload_start + packet_payload_bits

    return packets


def reassemble_stream_from_packets(packets: Sequence[AirPacket]) -> bytes:
    return b"".join(packet.payload for packet in packets)


def _normalize_robot_pair(values: Dict[str, Sequence[int]], names: Sequence[str]) -> bytes:
    packed = bytearray()
    for name in names:
        pair = values[name]
        packed += struct.pack("<HH", int(pair[0]), int(pair[1]))
    return bytes(packed)


def _normalize_u16_sequence(values: Dict[str, int], names: Sequence[str]) -> bytes:
    packed = bytearray()
    for name in names:
        packed += struct.pack("<H", int(values.get(name, 0)))
    return bytes(packed)


def _build_buff_payload(state: Dict[str, object]) -> bytes:
    payload = bytearray()
    buffs = state["buffs"]
    for name in BUFF_ORDER:
        item = buffs[name]
        payload += struct.pack(
            "<B H B B H",
            int(item["heal_pct"]),
            int(item["cooling"]),
            int(item["defense_pct"]),
            int(item["negative_defense_pct"]),
            int(item["attack_pct"]),
        )
    payload += struct.pack("<B", int(state["sentry_posture"]))
    return bytes(payload)


def build_information_round_stream(state: Dict[str, object], seq_start: int = 0) -> bytes:
    frames = [
        build_referee_frame(CMD_INFO_POSITION, _normalize_robot_pair(state["positions_cm"], POSITION_ORDER), seq_start + 0),
        build_referee_frame(CMD_INFO_HP, _normalize_u16_sequence(state["hp"], HP_ORDER), seq_start + 1),
        build_referee_frame(CMD_INFO_BULLET, _normalize_u16_sequence(state["bullet_allowance"], BULLET_ORDER), seq_start + 2),
        build_referee_frame(
            CMD_INFO_MACRO,
            struct.pack(
                "<H H I",
                int(state["coins_remaining"]),
                int(state["coins_total"]),
                int(state["occupancy_status_bits"]),
            ),
            seq_start + 3,
        ),
        build_referee_frame(CMD_INFO_BUFF, _build_buff_payload(state), seq_start + 4),
    ]
    stream = b"".join(frames)
    if len(stream) != 135:
        raise AssertionError(f"information round must be exactly 135 bytes, got {len(stream)}")
    return stream


def build_information_round_packets(state: Dict[str, object], seq_start: int = 0) -> List[AirPacket]:
    return build_air_packets(build_information_round_stream(state, seq_start=seq_start), INFO_ACCESS_CODE)


def build_jamming_round_stream(jam_key: str, seq: int = 0, fill_seed: int = 0) -> bytes:
    if len(jam_key) != 6 or not jam_key.isalnum():
        raise ValueError("jam key must be exactly 6 ASCII letters or digits")
    frame = build_referee_frame(CMD_JAM_KEY, jam_key.encode("ascii"), seq)
    if len(frame) != 15:
        raise AssertionError(f"0x0A06 frame must be 15 bytes, got {len(frame)}")
    filler = bytearray(random.Random(fill_seed).randrange(0, 256) for _ in range(120))
    return frame + bytes(filler)


def build_jamming_round_packets(jam_key: str, seq: int = 0, fill_seed: int = 0) -> List[AirPacket]:
    return build_air_packets(build_jamming_round_stream(jam_key, seq=seq, fill_seed=fill_seed), JAM_ACCESS_CODE)


def load_state(path: str | Path | None) -> Dict[str, object]:
    if path is None:
        path = Path(__file__).with_name("sample_state.json")
    state_path = Path(path)
    with state_path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def decode_payload(cmd_id: int, payload: bytes) -> Dict[str, object]:
    if cmd_id == CMD_INFO_POSITION:
        values = struct.unpack("<" + "H" * 12, payload)
        out: Dict[str, object] = {}
        for i, name in enumerate(POSITION_ORDER):
            out[name] = {"x_cm": values[i * 2], "y_cm": values[i * 2 + 1]}
        return out

    if cmd_id == CMD_INFO_HP:
        values = struct.unpack("<" + "H" * 6, payload)
        return {name: values[i] for i, name in enumerate(HP_ORDER)}

    if cmd_id == CMD_INFO_BULLET:
        values = struct.unpack("<" + "H" * 5, payload)
        return {name: values[i] for i, name in enumerate(BULLET_ORDER)}

    if cmd_id == CMD_INFO_MACRO:
        remaining, total, bits = struct.unpack("<H H I", payload)
        return {
            "coins_remaining": remaining,
            "coins_total": total,
            "occupancy_status_bits": bits,
        }

    if cmd_id == CMD_INFO_BUFF:
        cursor = 0
        result: Dict[str, object] = {}
        for name in BUFF_ORDER:
            heal_pct, cooling, defense_pct, negative_defense_pct, attack_pct = struct.unpack_from("<B H B B H", payload, cursor)
            result[name] = {
                "heal_pct": heal_pct,
                "cooling": cooling,
                "defense_pct": defense_pct,
                "negative_defense_pct": negative_defense_pct,
                "attack_pct": attack_pct,
            }
            cursor += 7
        result["sentry_posture"] = payload[cursor]
        return result

    if cmd_id == CMD_JAM_KEY:
        return {"jam_key": payload.decode("ascii", errors="replace")}

    return {"payload_hex": payload.hex(" ")}


def summarize_frames(frames: Iterable[RefereeFrame]) -> List[Dict[str, object]]:
    summary = []
    for frame in frames:
        summary.append(
            {
                "seq": frame.seq,
                "cmd_id": f"0x{frame.cmd_id:04X}",
                "payload_len": len(frame.payload),
                "crc8_mode": frame.crc8_mode,
                "decoded": decode_payload(frame.cmd_id, frame.payload),
            }
        )
    return summary


def profile_for(team: str, mode: str, jam_level: int = 1) -> Dict[str, float]:
    if mode == "info":
        return {
            "center_freq_hz": float(INFO_FREQUENCY_HZ[team]),
            "rf_bandwidth_hz": float(INFO_RF_BANDWIDTH_HZ),
            "sample_rate": float(INFO_SAMPLE_RATE),
            "sps": float(INFO_SPS),
            "bt": float(INFO_BT),
            "sensitivity": float(INFO_SENSITIVITY),
        }

    profile = JAM_PROFILES[team][jam_level]
    return {
        "center_freq_hz": float(profile["center_freq_hz"]),
        "rf_bandwidth_hz": float(profile["rf_bandwidth_hz"]),
        "sample_rate": float(INFO_SAMPLE_RATE),
        "sps": float(INFO_SPS),
        "bt": float(INFO_BT),
        "sensitivity": float(profile["sensitivity"]),
    }
