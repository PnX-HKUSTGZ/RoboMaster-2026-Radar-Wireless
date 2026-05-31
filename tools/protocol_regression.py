#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from wireless_link.protocol import (
    AIR_BIT_ORDER_LSB_ALL,
    AIR_BIT_ORDER_MSB_ALL,
    AIR_BIT_ORDER_OFFICIAL_1404,
    AIR_SECTION_ACCESS,
    CMD_JAM_KEY,
    CRC8_MODE_LEGACY,
    CRC8_MODE_REFLECTED,
    DEFAULT_AIR_BIT_ORDER,
    DEFAULT_TX_CRC8_MODE,
    build_referee_frame,
    bytes_to_air_bits,
    crc8_dji,
    crc8_dji_reflected,
    parse_referee_frame,
)
from wireless_link.referee_link import (
    CMD_ROBOT_INTERACTION,
    build_radar_key_verify_frame,
    build_robot_interaction_frame,
    parse_radar_info_payload,
)


def bits_text(bits: list[int]) -> str:
    return "".join(str(int(bit) & 1) for bit in bits)


def main() -> int:
    if DEFAULT_AIR_BIT_ORDER != AIR_BIT_ORDER_MSB_ALL:
        raise AssertionError(DEFAULT_AIR_BIT_ORDER)
    if DEFAULT_TX_CRC8_MODE != CRC8_MODE_REFLECTED:
        raise AssertionError(DEFAULT_TX_CRC8_MODE)

    field_header = bytes.fromhex("a5 06 00 21 6f")
    if crc8_dji_reflected(field_header[:4]) != field_header[4]:
        raise AssertionError("reflected CRC8 sample mismatch")
    if crc8_dji(field_header[:4]) == field_header[4]:
        raise AssertionError("legacy CRC8 unexpectedly accepts field sample")

    if bits_text(bytes_to_air_bits(b"\x2F", section=AIR_SECTION_ACCESS)) != "00101111":
        raise AssertionError("default 0x2F access bits mismatch")
    if bits_text(bytes_to_air_bits(b"\x2F", air_bit_order=AIR_BIT_ORDER_MSB_ALL, section=AIR_SECTION_ACCESS)) != "00101111":
        raise AssertionError("msb_all 0x2F mismatch")
    if bits_text(bytes_to_air_bits(b"\x2F", air_bit_order=AIR_BIT_ORDER_LSB_ALL, section=AIR_SECTION_ACCESS)) != "11110100":
        raise AssertionError("lsb_all 0x2F mismatch")
    if bits_text(bytes_to_air_bits(b"\x2F", air_bit_order=AIR_BIT_ORDER_OFFICIAL_1404, section=AIR_SECTION_ACCESS)) != "11110100":
        raise AssertionError("official_1404 0x2F mismatch")

    reflected_frame = build_referee_frame(CMD_JAM_KEY, b"fcYqTC", seq=0)
    if parse_referee_frame(reflected_frame).crc8_mode != CRC8_MODE_REFLECTED:
        raise AssertionError("default reflected frame parse failed")
    legacy_frame = build_referee_frame(CMD_JAM_KEY, b"fcYqTC", seq=0, crc8_mode=CRC8_MODE_LEGACY)
    if parse_referee_frame(legacy_frame).crc8_mode != CRC8_MODE_LEGACY:
        raise AssertionError("legacy frame compatibility failed")

    radar_info = parse_radar_info_payload(bytes([0b00110000]))
    if radar_info.jam_level != 2 or not radar_info.can_update_key:
        raise AssertionError(radar_info)
    upload = build_radar_key_verify_frame("fcYqTC", seq=7)
    parsed_upload = parse_referee_frame(upload)
    if parsed_upload.cmd_id != 0x0301:
        raise AssertionError("radar key upload frame mismatch")
    if parsed_upload.payload[:6] != bytes.fromhex("21016d008080"):
        raise AssertionError("radar key upload interaction header mismatch")
    interaction = build_robot_interaction_frame(
        data_cmd_id=0x0201,
        sender_id=109,
        receiver_id=107,
        user_data=bytes.fromhex("0101643412"),
        seq=8,
    )
    parsed_interaction = parse_referee_frame(interaction)
    if parsed_interaction.cmd_id != CMD_ROBOT_INTERACTION:
        raise AssertionError("robot interaction frame mismatch")
    if parsed_interaction.payload[:6] != bytes.fromhex("01026d006b00"):
        raise AssertionError("robot interaction header mismatch")

    print(json.dumps({
        "protocol_regression": "ok",
        "default_air_bit_order": DEFAULT_AIR_BIT_ORDER,
        "default_tx_crc8_mode": DEFAULT_TX_CRC8_MODE,
        "field_crc8_sample": "ok",
        "referee_upload_frame": "ok",
        "robot_interaction_frame": "ok",
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
