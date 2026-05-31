from __future__ import annotations

import struct
from collections import Counter
from typing import Iterable, Sequence

from .protocol import (
    AIR_BIT_ORDER_AUTO,
    AIR_BIT_ORDER_CHOICES,
    AIR_PAYLOAD_LEN,
    AIR_HEADER,
    AIR_SECTION_ACCESS,
    AIR_SECTION_HEADER,
    AIR_SECTION_PAYLOAD,
    DEFAULT_AIR_BIT_ORDER,
    INFO_ACCESS_CODE,
    JAM_ACCESS_CODE,
    AirPacket,
    air_bits_to_bytes,
    bytes_to_air_bits,
    decode_payload,
    detect_crc8_mode,
    iter_referee_frames,
    parse_referee_frame,
    BULLET_ORDER,
    POSITION_ORDER,
    reassemble_stream_from_packets,
    recover_air_packets_from_bits,
    summarize_frames,
    verify_crc16,
)

DEFAULT_RECENT_DECODE_BITS = 240_000

DECODE_MODE_EXACT = "exact"
DECODE_MODE_FUZZY_ACCESS = "fuzzy_access"
DECODE_MODE_CHOICES = (DECODE_MODE_EXACT, DECODE_MODE_FUZZY_ACCESS)

FUZZY_ACCESS_MAX_HAMMING = 2
JAM_KEY_PRINTABLE_MIN_VOTES = 2
INFO_PARTIAL_CMD_IDS = {0x0A01, 0x0A02, 0x0A03, 0x0A04, 0x0A05}
DEFAULT_PARTIAL_FRAME_LIMIT = 32
INFO_REMAINING_BULLET_OUTPUT_ORDER = ("hero", "infantry_3", "infantry_4", "sentry", "aerial")


def access_code_for_mode(mode: str) -> bytes:
    if mode == "info":
        return INFO_ACCESS_CODE
    if mode == "jam":
        return JAM_ACCESS_CODE
    raise ValueError(f"unsupported wireless-link mode: {mode}")


def access_code_diagnostics(bit_items: list[int], access_code: bytes, air_bit_order: str, top_n: int = 5) -> dict:
    access_bits = bytes_to_air_bits(access_code, air_bit_order=air_bit_order, section=AIR_SECTION_ACCESS)
    width = len(access_bits)
    if len(bit_items) < width:
        return {"min_hamming": None, "candidates": []}
    best: list[tuple[int, int]] = []
    for offset in range(0, len(bit_items) - width + 1):
        distance = sum((bit_items[offset + i] & 1) != access_bits[i] for i in range(width))
        if len(best) < top_n:
            best.append((distance, offset))
            best.sort()
        elif distance < best[-1][0]:
            best[-1] = (distance, offset)
            best.sort()
    return {
        "min_hamming": best[0][0],
        "candidates": [{"hamming": distance, "bit_offset": offset} for distance, offset in best],
    }


def recover_air_packets_from_bits_fuzzy_access(
    bit_items: Sequence[int],
    access_code: bytes,
    *,
    air_bit_order: str = DEFAULT_AIR_BIT_ORDER,
    max_hamming: int = FUZZY_ACCESS_MAX_HAMMING,
) -> list[AirPacket]:
    access_bits = bytes_to_air_bits(access_code, air_bit_order=air_bit_order, section=AIR_SECTION_ACCESS)
    packet_payload_bits = AIR_PAYLOAD_LEN * 8
    packet_header_bits = 32
    packets: list[AirPacket] = []
    cursor = 0
    max_start = len(bit_items) - (len(access_bits) + packet_header_bits + packet_payload_bits)

    while cursor <= max_start:
        distance = sum((bit_items[cursor + i] & 1) != access_bits[i] for i in range(len(access_bits)))
        if distance > max_hamming:
            cursor += 1
            continue

        header_start = cursor + len(access_bits)
        header_bits = bit_items[header_start:header_start + packet_header_bits]
        header = air_bits_to_bytes(header_bits, air_bit_order=air_bit_order, section=AIR_SECTION_HEADER)
        try:
            length_a, length_b = struct.unpack(">HH", header)
        except struct.error:
            cursor += 1
            continue
        if length_a != AIR_PAYLOAD_LEN or length_b != AIR_PAYLOAD_LEN:
            cursor += 1
            continue

        payload_start = header_start + packet_header_bits
        payload_bits = bit_items[payload_start:payload_start + packet_payload_bits]
        payload = air_bits_to_bytes(payload_bits, air_bit_order=air_bit_order, section=AIR_SECTION_PAYLOAD)
        packets.append(AirPacket(access_code=access_code, header=AIR_HEADER, payload=payload))
        cursor = payload_start + packet_payload_bits

    return packets


def _is_plausible_info_payload(cmd_id: int, payload: bytes) -> tuple[bool, str]:
    try:
        if cmd_id == 0x0A01:
            values = struct.unpack("<" + "H" * 12, payload)
            return all(0 <= value <= 3000 for value in values), f"coords_cm={values}"
        if cmd_id == 0x0A02:
            values = struct.unpack("<" + "H" * 6, payload)
            return all(0 <= value <= 2000 for value in values), f"hp={values}"
        if cmd_id == 0x0A03:
            values = struct.unpack("<" + "H" * 5, payload)
            return all(0 <= value <= 1000 for value in values), f"bullet={values}"
        if cmd_id == 0x0A04:
            remaining, total, bits = struct.unpack("<H H I", payload)
            return remaining <= 1000 and total <= 2000 and bits < (1 << 20), f"macro={(remaining, total, bits)}"
        if cmd_id == 0x0A05:
            return all(value <= 200 for value in payload), "buff_bytes<=200"
    except Exception as exc:
        return False, f"plausibility_error:{exc}"
    return False, "unsupported_cmd_id"


def scan_partial_info_frames(
    packets: Sequence[AirPacket],
    *,
    max_frames: int = DEFAULT_PARTIAL_FRAME_LIMIT,
) -> list[dict]:
    stream = reassemble_stream_from_packets(packets)
    frames: list[dict] = []
    for stream_offset, value in enumerate(stream):
        if value != 0xA5 or stream_offset + 9 > len(stream):
            continue
        data_len = struct.unpack_from("<H", stream, stream_offset + 1)[0]
        frame_len = 5 + 2 + data_len + 2
        if frame_len < 9 or stream_offset + frame_len > len(stream):
            continue
        raw = stream[stream_offset:stream_offset + frame_len]
        crc8_mode = detect_crc8_mode(raw[:5])
        if not crc8_mode or not verify_crc16(raw):
            continue
        try:
            parsed = parse_referee_frame(raw)
        except ValueError:
            continue
        if parsed.cmd_id not in INFO_PARTIAL_CMD_IDS:
            continue
        plausible, reason = _is_plausible_info_payload(parsed.cmd_id, parsed.payload)
        try:
            decoded = decode_payload(parsed.cmd_id, parsed.payload)
        except Exception as exc:
            decoded = {"decode_error": str(exc), "payload_hex": parsed.payload.hex(" ")}
        frames.append(
            {
                "stream_offset": stream_offset,
                "packet_index": stream_offset // AIR_PAYLOAD_LEN,
                "seq": parsed.seq,
                "cmd_id": f"0x{parsed.cmd_id:04X}",
                "payload_len": len(parsed.payload),
                "crc8_mode": crc8_mode,
                "plausible": plausible,
                "plausibility_reason": reason,
                "payload_hex": parsed.payload.hex(" "),
                "decoded": decoded,
            }
        )
        if len(frames) >= max_frames:
            break
    return frames


def extract_info_positions(frames: Sequence[dict], partial_frames: Sequence[dict] = ()) -> tuple[dict, dict]:
    positions: dict[str, list[int]] = {}
    sources: dict[str, str] = {}

    def add_positions(decoded: object, source: str) -> None:
        if not isinstance(decoded, dict):
            return
        for name in POSITION_ORDER:
            item = decoded.get(name)
            if not isinstance(item, dict):
                continue
            x_val = int(item.get("x_cm", 0) or 0)
            y_val = int(item.get("y_cm", 0) or 0)
            if x_val == 0 and y_val == 0:
                continue
            positions[name] = [x_val, y_val]
            sources[name] = source

    for frame in frames:
        if frame.get("cmd_id") == "0x0A01":
            add_positions(frame.get("decoded"), "full")
    if positions:
        return positions, sources

    for frame in partial_frames:
        if frame.get("cmd_id") == "0x0A01" and frame.get("plausible"):
            add_positions(frame.get("decoded"), "partial")
    return positions, sources


def _candidate_info_frames(
    frames: Sequence[dict],
    partial_frames: Sequence[dict],
    cmd_id: str,
) -> list[tuple[dict, str]]:
    full = [(frame, "full") for frame in frames if frame.get("cmd_id") == cmd_id]
    if full:
        return full
    return [
        (frame, "partial")
        for frame in partial_frames
        if frame.get("cmd_id") == cmd_id and bool(frame.get("plausible"))
    ]


def extract_info_economics(frames: Sequence[dict], partial_frames: Sequence[dict] = ()) -> dict:
    economics: dict[str, object] = {}
    for frame, source in _candidate_info_frames(frames, partial_frames, "0x0A04"):
        decoded = frame.get("decoded")
        if not isinstance(decoded, dict):
            continue
        try:
            remaining = int(decoded.get("coins_remaining", 0) or 0)
            total = int(decoded.get("coins_total", 0) or 0)
            bits = int(decoded.get("occupancy_status_bits", 0) or 0)
        except (TypeError, ValueError):
            continue
        economics = {
            "remaining": remaining,
            "total": total,
            "occupancy_status_bits": bits,
            "source": source,
        }
    return economics


def extract_info_remaining_bullets(frames: Sequence[dict], partial_frames: Sequence[dict] = ()) -> dict:
    bullets: dict[str, object] = {}
    for frame, source in _candidate_info_frames(frames, partial_frames, "0x0A03"):
        decoded = frame.get("decoded")
        if not isinstance(decoded, dict):
            continue
        try:
            next_bullets = {
                name: int(decoded.get(name, 0) or 0)
                for name in INFO_REMAINING_BULLET_OUTPUT_ORDER
                if name in decoded or name in BULLET_ORDER
            }
        except (TypeError, ValueError):
            continue
        if not next_bullets:
            continue
        next_bullets["source"] = source
        bullets = next_bullets
    return bullets


def _decode_frames_once(
    bit_items: list[int],
    access_code: bytes,
    air_bit_order: str = DEFAULT_AIR_BIT_ORDER,
    decode_mode: str = DECODE_MODE_EXACT,
    fuzzy_access_max_hamming: int = FUZZY_ACCESS_MAX_HAMMING,
) -> tuple[list[dict], int]:
    if decode_mode == DECODE_MODE_EXACT:
        packets = recover_air_packets_from_bits(bit_items, access_code=access_code, air_bit_order=air_bit_order)
    elif decode_mode == DECODE_MODE_FUZZY_ACCESS:
        packets = recover_air_packets_from_bits_fuzzy_access(
            bit_items,
            access_code=access_code,
            air_bit_order=air_bit_order,
            max_hamming=fuzzy_access_max_hamming,
        )
    else:
        raise ValueError(f"unsupported decode_mode: {decode_mode}")
    frames = summarize_frames(iter_referee_frames(reassemble_stream_from_packets(packets)))
    return frames, len(packets)


def _is_probable_jam_payload(payload: bytes) -> bool:
    if len(payload) != AIR_PAYLOAD_LEN:
        return False
    if payload[0] != 0xA5:
        return False
    if sum((payload[index] ^ expected).bit_count() for index, expected in ((5, 0x06), (6, 0x0A))) > 4:
        return False
    key = payload[7:13]
    printable = sum(48 <= item <= 57 or 65 <= item <= 90 or 97 <= item <= 122 for item in key)
    return printable >= 4


def jam_key_candidates_from_packets(packets: Sequence[AirPacket]) -> list[bytes]:
    return [bytes(packet.payload[7:13]) for packet in packets if _is_probable_jam_payload(packet.payload)]


def recover_jam_key_from_candidates(
    candidates: Sequence[bytes],
    *,
    min_votes: int = JAM_KEY_PRINTABLE_MIN_VOTES,
    source: str = "packet_vote",
) -> dict:
    candidates = [bytes(candidate) for candidate in candidates if len(candidate) == 6]
    candidate_keys_hex = [candidate.hex() for candidate in candidates]
    if not candidates:
        return {
            "recovered_key": None,
            "confidence": 0.0,
            "candidate_count": 0,
            "byte_votes": [],
            "candidate_keys_hex": [],
            "source": source,
        }

    recovered = bytearray()
    byte_votes = []
    for byte_index in range(6):
        counts = Counter(candidate[byte_index] for candidate in candidates)
        printable_counts = {
            value: count
            for value, count in counts.items()
            if 48 <= value <= 57 or 65 <= value <= 90 or 97 <= value <= 122
        }
        if not printable_counts:
            return {
                "recovered_key": None,
                "confidence": 0.0,
                "candidate_count": len(candidates),
                "byte_votes": byte_votes,
                "candidate_keys_hex": candidate_keys_hex,
                "source": source,
            }
        value, count = max(printable_counts.items(), key=lambda item: (item[1], item[0]))
        recovered.append(value)
        byte_votes.append({"index": byte_index, "byte": value, "char": chr(value), "votes": count})

    min_vote_count = min(item["votes"] for item in byte_votes)
    confidence = min_vote_count / max(1, len(candidates))
    key = recovered.decode("ascii", errors="replace")
    if min_vote_count < min_votes or not key.isalnum():
        key = None
    return {
        "recovered_key": key,
        "confidence": float(confidence),
        "candidate_count": len(candidates),
        "byte_votes": byte_votes,
        "candidate_keys_hex": candidate_keys_hex,
        "source": source,
    }


def recover_jam_key_from_packets(packets: Sequence[AirPacket], *, min_votes: int = JAM_KEY_PRINTABLE_MIN_VOTES) -> dict:
    return recover_jam_key_from_candidates(
        jam_key_candidates_from_packets(packets),
        min_votes=min_votes,
        source="packet_vote",
    )


def summarize_air_packets(packets: Sequence[AirPacket], max_packets: int = 12) -> list[dict]:
    summary = []
    for index, packet in enumerate(packets[:max_packets]):
        payload = bytes(packet.payload)
        nonzero_count = sum(1 for item in payload if item != 0)
        sof_offsets = [offset for offset, item in enumerate(payload) if item == 0xA5]
        summary.append(
            {
                "index": index,
                "payload_hex": payload.hex(" "),
                "nonzero_count": nonzero_count,
                "sof_offsets": sof_offsets,
            }
        )
    return summary


def decode_recent_bits(
    bit_items: Iterable[int],
    mode: str,
    recent_limit: int = DEFAULT_RECENT_DECODE_BITS,
    air_bit_order: str = DEFAULT_AIR_BIT_ORDER,
    decode_mode: str = DECODE_MODE_EXACT,
    fuzzy_access_max_hamming: int = FUZZY_ACCESS_MAX_HAMMING,
    jam_key_min_votes: int = JAM_KEY_PRINTABLE_MIN_VOTES,
) -> dict:
    if decode_mode not in DECODE_MODE_CHOICES:
        raise ValueError(f"unsupported decode_mode: {decode_mode}")
    bits = [int(item) & 0x01 for item in bit_items]
    if recent_limit > 0:
        bits = bits[-recent_limit:]

    access_code = access_code_for_mode(mode)

    if air_bit_order == AIR_BIT_ORDER_AUTO:
        candidates = [
            decode_recent_bits(
                bits,
                mode=mode,
                recent_limit=0,
                air_bit_order=order,
                decode_mode=decode_mode,
                fuzzy_access_max_hamming=fuzzy_access_max_hamming,
                jam_key_min_votes=jam_key_min_votes,
            )
            for order in AIR_BIT_ORDER_CHOICES
        ]
        candidates.sort(
            key=lambda item: (
                item["referee_frames_found"],
                len(item.get("info_positions_cm", {})) if mode == "info" else 0,
                sum(int(value) for value in item.get("plausible_partial_frame_counts", {}).values()) if mode == "info" else 0,
                int(item.get("partial_referee_frames_found", 0)) if mode == "info" else 0,
                item["air_packets_found"],
                -min(
                    item["access_code_diagnostics"]["normal"].get("min_hamming") or 9999,
                    item["access_code_diagnostics"]["inverted"].get("min_hamming") or 9999,
                ),
            ),
            reverse=True,
        )
        selected = dict(candidates[0])
        selected["air_bit_order"] = AIR_BIT_ORDER_AUTO
        selected["selected_air_bit_order"] = candidates[0]["selected_air_bit_order"]
        selected["air_bit_order_candidates"] = [
            {
                "selected_air_bit_order": candidate["selected_air_bit_order"],
                "referee_frames_found": candidate["referee_frames_found"],
                "air_packets_found": candidate["air_packets_found"],
                "bit_polarity": candidate["bit_polarity"],
                "frame_counts": candidate["frame_counts"],
                "access_code_diagnostics": candidate["access_code_diagnostics"],
            }
            for candidate in candidates
        ]
        return selected

    normal_frames, normal_packets = _decode_frames_once(
        bits,
        access_code,
        air_bit_order=air_bit_order,
        decode_mode=decode_mode,
        fuzzy_access_max_hamming=fuzzy_access_max_hamming,
    )
    inverted_bits = [bit ^ 1 for bit in bits]
    inverted_frames, inverted_packets = _decode_frames_once(
        inverted_bits,
        access_code,
        air_bit_order=air_bit_order,
        decode_mode=decode_mode,
        fuzzy_access_max_hamming=fuzzy_access_max_hamming,
    )

    use_inverted = (len(inverted_frames), inverted_packets) > (len(normal_frames), normal_packets)
    frames = inverted_frames if use_inverted else normal_frames
    packet_count = inverted_packets if use_inverted else normal_packets
    counts = Counter(frame["cmd_id"] for frame in frames)
    selected_bits = inverted_bits if use_inverted else bits
    if decode_mode == DECODE_MODE_FUZZY_ACCESS:
        selected_packets = recover_air_packets_from_bits_fuzzy_access(
            selected_bits,
            access_code=access_code,
            air_bit_order=air_bit_order,
            max_hamming=fuzzy_access_max_hamming,
        )
    else:
        selected_packets = recover_air_packets_from_bits(
            selected_bits,
            access_code=access_code,
            air_bit_order=air_bit_order,
        )
    recovered_jam_key = (
        recover_jam_key_from_packets(selected_packets, min_votes=jam_key_min_votes)
        if mode == "jam"
        else None
    )
    partial_info_frames = scan_partial_info_frames(selected_packets) if mode == "info" else []
    partial_counts = Counter(frame["cmd_id"] for frame in partial_info_frames)
    plausible_partial_counts = Counter(frame["cmd_id"] for frame in partial_info_frames if frame.get("plausible"))
    info_positions_cm, info_positions_source = (
        extract_info_positions(frames, partial_info_frames) if mode == "info" else ({}, {})
    )
    info_economics = extract_info_economics(frames, partial_info_frames) if mode == "info" else {}
    info_remaining_bullets = extract_info_remaining_bullets(frames, partial_info_frames) if mode == "info" else {}
    return {
        "frames": frames,
        "air_packets_found": packet_count,
        "air_packet_payloads": summarize_air_packets(selected_packets),
        "referee_frames_found": len(frames),
        "partial_referee_frames_found": len(partial_info_frames),
        "partial_referee_frames": partial_info_frames,
        "partial_frame_counts": dict(sorted(partial_counts.items())),
        "plausible_partial_frame_counts": dict(sorted(plausible_partial_counts.items())),
        "info_positions_cm": info_positions_cm,
        "info_positions_source": info_positions_source,
        "info_economics": info_economics,
        "info_remaining_bullets": info_remaining_bullets,
        "bit_polarity": "inverted" if use_inverted else "normal",
        "air_bit_order": air_bit_order,
        "decode_mode": decode_mode,
        "selected_air_bit_order": air_bit_order,
        "access_code_diagnostics": {
            "normal": access_code_diagnostics(bits, access_code, air_bit_order),
            "inverted": access_code_diagnostics(inverted_bits, access_code, air_bit_order),
        },
        "frame_counts": dict(sorted(counts.items())),
        "recovered_jam_key": recovered_jam_key,
        "demod_bits": len(bits),
    }


def strict_jam_key(result: dict) -> str | None:
    for frame in result.get("frames", []):
        if frame.get("cmd_id") == "0x0A06":
            key = frame.get("decoded", {}).get("jam_key")
            if isinstance(key, str) and len(key) == 6 and key.isalnum():
                return key
    return None
