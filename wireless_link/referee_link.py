from __future__ import annotations

import os
import select
import struct
import termios
import time
import tty
from dataclasses import dataclass
from typing import Iterable

from .protocol import REF_SOF, RefereeFrame, build_referee_frame, parse_referee_frame

CMD_RADAR_INFO = 0x020E
CMD_RADAR_CMD = 0x0121
CMD_ROBOT_INTERACTION = 0x0301
SERVER_ROBOT_ID = 0x8080
RED_RADAR_ROBOT_ID = 9
BLUE_RADAR_ROBOT_ID = 109

DEFAULT_REFEREE_PORT = "/dev/ttyACM0"
DEFAULT_REFEREE_BAUD = 115200
RADAR_PASSWORD_VERIFY_CMD = 2
KEY_UPLOAD_THROTTLE_S = 10.0


@dataclass(frozen=True)
class RadarInfo:
    raw: int
    double_vulnerability_chances: int
    double_vulnerability_active: bool
    jam_level_raw: int
    jam_level: int
    can_update_key: bool


def parse_radar_info_payload(payload: bytes) -> RadarInfo:
    if len(payload) < 1:
        raise ValueError("0x020E radar_info payload must contain at least 1 byte")
    value = int(payload[0])
    jam_level_raw = (value >> 3) & 0x03
    return RadarInfo(
        raw=value,
        double_vulnerability_chances=value & 0x03,
        double_vulnerability_active=bool(value & (1 << 2)),
        jam_level_raw=jam_level_raw,
        jam_level=max(1, min(3, jam_level_raw)),
        can_update_key=bool(value & (1 << 5)),
    )


def radar_info_to_dict(info: RadarInfo | None) -> dict:
    if info is None:
        return {
            "referee_jam_level": None,
            "referee_jam_level_raw": None,
            "referee_can_update_key": None,
            "referee_double_vulnerability_chances": None,
            "referee_double_vulnerability_active": None,
        }
    return {
        "referee_jam_level": info.jam_level,
        "referee_jam_level_raw": info.jam_level_raw,
        "referee_can_update_key": info.can_update_key,
        "referee_double_vulnerability_chances": info.double_vulnerability_chances,
        "referee_double_vulnerability_active": info.double_vulnerability_active,
    }


def build_radar_cmd_payload(*, radar_cmd: int = 0, password_cmd: int = RADAR_PASSWORD_VERIFY_CMD, key: str = "000000") -> bytes:
    if len(key) != 6 or not key.isalnum() or not key.isascii():
        raise ValueError("radar password key must be exactly 6 ASCII letters or digits")
    return struct.pack("<BB6s", int(radar_cmd) & 0xFF, int(password_cmd) & 0xFF, key.encode("ascii"))


def build_radar_key_verify_frame(key: str, *, seq: int = 0, radar_cmd: int = 0) -> bytes:
    return build_radar_key_verify_interaction_frame(key, seq=seq, radar_cmd=radar_cmd)


def build_legacy_radar_key_verify_frame(key: str, *, seq: int = 0, radar_cmd: int = 0) -> bytes:
    return build_referee_frame(
        CMD_RADAR_CMD,
        build_radar_cmd_payload(radar_cmd=radar_cmd, password_cmd=RADAR_PASSWORD_VERIFY_CMD, key=key),
        seq=seq,
    )


def build_robot_interaction_payload(
    *,
    data_cmd_id: int,
    sender_id: int,
    receiver_id: int,
    user_data: bytes,
) -> bytes:
    if not 0 <= int(data_cmd_id) <= 0xFFFF:
        raise ValueError("data_cmd_id must be uint16")
    if not 0 <= int(sender_id) <= 0xFFFF:
        raise ValueError("sender_id must be uint16")
    if not 0 <= int(receiver_id) <= 0xFFFF:
        raise ValueError("receiver_id must be uint16")
    if len(user_data) > 112:
        raise ValueError("robot interaction user_data must be <= 112 bytes")
    return struct.pack("<HHH", int(data_cmd_id), int(sender_id), int(receiver_id)) + bytes(user_data)


def build_robot_interaction_frame(
    *,
    data_cmd_id: int,
    sender_id: int,
    receiver_id: int,
    user_data: bytes,
    seq: int = 0,
) -> bytes:
    return build_referee_frame(
        CMD_ROBOT_INTERACTION,
        build_robot_interaction_payload(
            data_cmd_id=data_cmd_id,
            sender_id=sender_id,
            receiver_id=receiver_id,
            user_data=user_data,
        ),
        seq=seq,
    )


def build_radar_key_verify_interaction_frame(
    key: str,
    *,
    seq: int = 0,
    radar_cmd: int = 0,
    sender_id: int = BLUE_RADAR_ROBOT_ID,
) -> bytes:
    return build_robot_interaction_frame(
        data_cmd_id=CMD_RADAR_CMD,
        sender_id=int(sender_id),
        receiver_id=SERVER_ROBOT_ID,
        user_data=build_radar_cmd_payload(
            radar_cmd=radar_cmd,
            password_cmd=RADAR_PASSWORD_VERIFY_CMD,
            key=key,
        ),
        seq=seq,
    )


class RefereeFrameStream:
    def __init__(self) -> None:
        self.buffer = bytearray()

    def feed(self, data: bytes) -> list[RefereeFrame]:
        if data:
            self.buffer.extend(data)
        frames: list[RefereeFrame] = []
        while len(self.buffer) >= 9:
            try:
                sof_index = self.buffer.index(REF_SOF)
            except ValueError:
                self.buffer.clear()
                break
            if sof_index:
                del self.buffer[:sof_index]
            if len(self.buffer) < 5:
                break
            payload_len = struct.unpack_from("<H", self.buffer, 1)[0]
            frame_len = 5 + 2 + payload_len + 2
            if frame_len < 9:
                del self.buffer[0]
                continue
            if len(self.buffer) < frame_len:
                break
            candidate = bytes(self.buffer[:frame_len])
            del self.buffer[:frame_len]
            try:
                frames.append(parse_referee_frame(candidate))
            except ValueError:
                continue
        if len(self.buffer) > 4096:
            del self.buffer[:-512]
        return frames


class _PosixSerial:
    def __init__(self, port: str, baud: int) -> None:
        if baud != 115200:
            raise ValueError("POSIX fallback currently supports only 115200 baud")
        self.fd = os.open(port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        attrs = termios.tcgetattr(self.fd)
        tty.setraw(self.fd)
        attrs = termios.tcgetattr(self.fd)
        attrs[4] = termios.B115200
        attrs[5] = termios.B115200
        attrs[2] |= termios.CLOCAL | termios.CREAD
        attrs[2] &= ~termios.CSTOPB
        attrs[2] &= ~termios.PARENB
        if hasattr(termios, "CRTSCTS"):
            attrs[2] &= ~termios.CRTSCTS
        attrs[3] = 0
        attrs[6][termios.VMIN] = 0
        attrs[6][termios.VTIME] = 0
        termios.tcsetattr(self.fd, termios.TCSANOW, attrs)

    def read(self, size: int) -> bytes:
        readable, _, _ = select.select([self.fd], [], [], 0.0)
        if not readable:
            return b""
        try:
            return os.read(self.fd, size)
        except BlockingIOError:
            return b""

    def write(self, data: bytes) -> int:
        return os.write(self.fd, data)

    def close(self) -> None:
        os.close(self.fd)


class RefereeSerial:
    def __init__(self, port: str = DEFAULT_REFEREE_PORT, baud: int = DEFAULT_REFEREE_BAUD) -> None:
        self.port = port
        self.baud = int(baud)
        self.backend_name = "closed"
        self._serial = None
        self.last_error: str | None = None

    def open(self) -> None:
        try:
            import serial  # type: ignore

            self._serial = serial.Serial(
                self.port,
                self.baud,
                bytesize=8,
                parity="N",
                stopbits=1,
                timeout=0,
                write_timeout=0.02,
                rtscts=False,
                dsrdtr=False,
                xonxoff=False,
            )
            self.backend_name = "pyserial"
        except ModuleNotFoundError:
            self._serial = _PosixSerial(self.port, self.baud)
            self.backend_name = "posix"

    @property
    def is_open(self) -> bool:
        return self._serial is not None

    def read_available(self, size: int = 4096) -> bytes:
        if self._serial is None:
            return b""
        try:
            data = self._serial.read(size)
            self.last_error = None
            return bytes(data)
        except Exception as exc:
            self.last_error = str(exc)
            return b""

    def write(self, data: bytes) -> bool:
        if self._serial is None:
            self.last_error = "serial not open"
            return False
        try:
            self._serial.write(data)
            self.last_error = None
            return True
        except Exception as exc:
            self.last_error = str(exc)
            return False

    def close(self) -> None:
        if self._serial is None:
            return
        try:
            self._serial.close()
        finally:
            self._serial = None
            self.backend_name = "closed"


class RefereeLink:
    def __init__(self, port: str = DEFAULT_REFEREE_PORT, baud: int = DEFAULT_REFEREE_BAUD) -> None:
        self.serial = RefereeSerial(port=port, baud=baud)
        self.stream = RefereeFrameStream()
        self.latest_radar_info: RadarInfo | None = None
        self.seq = 0
        self.last_uploaded_key: str | None = None
        self.last_upload_time = 0.0
        self.last_error: str | None = None

    def open(self) -> None:
        self.serial.open()

    def close(self) -> None:
        self.serial.close()

    @property
    def online(self) -> bool:
        return self.serial.is_open and self.last_error is None

    def poll(self) -> list[RefereeFrame]:
        frames = self.stream.feed(self.serial.read_available())
        for frame in frames:
            if frame.cmd_id == CMD_RADAR_INFO:
                try:
                    self.latest_radar_info = parse_radar_info_payload(frame.payload)
                    self.last_error = None
                except ValueError as exc:
                    self.last_error = str(exc)
        if self.serial.last_error:
            self.last_error = self.serial.last_error
        return frames

    def upload_key(self, key: str, *, now: float | None = None, radar_cmd: int = 0, force: bool = False) -> dict:
        if now is None:
            now = time.monotonic()
        if not force and self.last_uploaded_key == key and now - self.last_upload_time < KEY_UPLOAD_THROTTLE_S:
            return {"sent": False, "reason": "throttled_same_key", "key": key}
        frame = build_radar_key_verify_frame(key, seq=self.seq, radar_cmd=radar_cmd)
        self.seq = (self.seq + 1) & 0xFF
        sent = self.serial.write(frame)
        if sent:
            self.last_uploaded_key = key
            self.last_upload_time = now
            return {"sent": True, "reason": "ok", "key": key, "bytes": len(frame)}
        return {"sent": False, "reason": self.serial.last_error or "write_failed", "key": key}


def _self_test() -> None:
    for raw, expected_level, expected_can_update in ((0b00101000, 1, True), (0b00110000, 2, True), (0b00011000, 3, False)):
        info = parse_radar_info_payload(bytes([raw]))
        if info.jam_level != expected_level or info.can_update_key != expected_can_update:
            raise AssertionError((raw, info))
    frame = build_radar_key_verify_interaction_frame("fcYqTC", seq=7)
    parsed = parse_referee_frame(frame)
    expected_payload = build_robot_interaction_payload(
        data_cmd_id=CMD_RADAR_CMD,
        sender_id=BLUE_RADAR_ROBOT_ID,
        receiver_id=SERVER_ROBOT_ID,
        user_data=build_radar_cmd_payload(key="fcYqTC"),
    )
    if parsed.cmd_id != CMD_ROBOT_INTERACTION or parsed.payload != expected_payload:
        raise AssertionError("radar key verify frame mismatch")
    interaction = build_robot_interaction_frame(
        data_cmd_id=0x0201,
        sender_id=109,
        receiver_id=107,
        user_data=bytes.fromhex("0101643412"),
        seq=8,
    )
    parsed_interaction = parse_referee_frame(interaction)
    if parsed_interaction.cmd_id != CMD_ROBOT_INTERACTION:
        raise AssertionError("robot interaction cmd_id mismatch")
    if parsed_interaction.payload != struct.pack("<HHH", 0x0201, 109, 107) + bytes.fromhex("0101643412"):
        raise AssertionError("robot interaction payload mismatch")


if __name__ == "__main__":
    _self_test()
    print("referee_link self-test passed")
