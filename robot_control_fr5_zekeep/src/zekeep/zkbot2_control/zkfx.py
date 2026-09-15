#!/usr/bin/env python3
"""
zkfx.py - ZK3U(FX3U 호환) PLC 통신 공용 모듈

미쓰비시 FX 프로그래밍 프로토콜(포맷1), 9600 7E1.
★ 모든 응답의 체크섬을 검증한다. 검증 실패 프레임은 버리고 재시도.
  (체크섬을 안 보면 깨진 프레임을 실제 상태로 오독한다 — 실제로 겪었음)

주소 (FX 메모리맵, 바이트 주소)
    X 0x0080 / Y 0x00A0 / M 0x0100 / D 0x1000+2n / D8000 0x0E00+2n
강제 ON/OFF 주소 (로우바이트 먼저)
    M n = 0x0800+n,  Y(8진→10진) = 0x0500+o,  X = 0x0400+o
"""
import os
import time

import serial

STX, ETX, ENQ, ACK, NAK = 0x02, 0x03, 0x05, 0x06, 0x15

A_X, A_Y, A_M, A_D, A_D8 = 0x0080, 0x00A0, 0x0100, 0x1000, 0x0E00

XN = {0: "A1리밋(베이스)", 1: "A2리밋(큰팔)", 2: "A3리밋(작은팔)",
      3: "START", 4: "ORIGIN", 5: "급정지", 6: "INFRA"}
YN = {0: "A1펄스", 1: "A2펄스", 2: "A3펄스", 3: "ENG펄스", 4: "A1방향",
      5: "A2방향", 6: "A3방향", 8: "A1_ENA", 9: "A2_ENA", 10: "A3_ENA",
      12: "펌프", 13: "밸브"}
PULSE_DIR = {0, 1, 2, 4, 5, 6}
PUMP, VALVE = 0o14, 0o15


def _cs(p):
    return f"{sum(p) & 0xFF:02X}".encode()


class ZK:
    def __init__(self, port=None):
        port = port or os.environ.get("ZKBOT2_PORT", "/dev/ttyUSB0")
        self.ser = serial.Serial(port, 9600, bytesize=7, parity="E", stopbits=1,
                                 timeout=0.05, write_timeout=1.0)
        self.stats = {"ok": 0, "csum": 0, "noresp": 0}

    # ---------- 저수준 ----------
    def _tx(self, body, expect_len=None, budget=0.5):
        self.ser.reset_input_buffer()
        self.ser.reset_output_buffer()
        self.ser.write(bytes([STX]) + body + _cs(body))
        self.ser.flush()
        buf, t = b"", time.time()
        while time.time() - t < budget:
            buf += self.ser.read(128)
            if expect_len is None:
                if buf[:1] in (bytes([ACK]), bytes([NAK])):
                    break
            else:
                s = buf.find(bytes([STX]))
                if s >= 0 and len(buf) - s >= expect_len:
                    break
        return buf

    def read(self, addr, n, tries=6):
        """체크섬 검증된 n바이트. 실패 시 None."""
        body = b"0" + f"{addr:04X}".encode() + f"{n:02X}".encode() + bytes([ETX])
        for _ in range(tries):
            buf = self._tx(body, expect_len=1 + n * 2 + 3)
            s = buf.find(bytes([STX]))
            if s < 0:
                self.stats["noresp"] += 1
                continue
            e = buf.find(bytes([ETX]), s)
            if e < 0 or len(buf) < e + 3:
                self.stats["noresp"] += 1
                continue
            payload = buf[s + 1:e]
            if buf[e + 1:e + 3].upper() != _cs(payload + bytes([ETX])).upper():
                self.stats["csum"] += 1
                continue
            try:
                v = bytes.fromhex(payload.decode("ascii"))
            except Exception:
                self.stats["csum"] += 1
                continue
            if len(v) != n:
                self.stats["csum"] += 1
                continue
            self.stats["ok"] += 1
            return v
        return None

    def write(self, addr, payload, tries=3):
        body = (b"1" + f"{addr:04X}".encode() + f"{len(payload):02X}".encode()
                + payload.hex().upper().encode() + bytes([ETX]))
        for _ in range(tries):
            buf = self._tx(body)
            if buf[:1] == bytes([ACK]):
                return True
            if buf[:1] == bytes([NAK]):
                return False
        return False

    def _force(self, cmd, code, tries=3):
        body = (cmd.encode()
                + f"{code & 0xFF:02X}{(code >> 8) & 0xFF:02X}".encode()
                + bytes([ETX]))
        for _ in range(tries):
            buf = self._tx(body)
            if buf[:1] == bytes([ACK]):
                return True
            if buf[:1] == bytes([NAK]):
                return False
        return False

    # ---------- 디바이스 ----------
    def m_on(self, n):
        return self._force("7", 0x0800 + n)

    def m_off(self, n):
        return self._force("8", 0x0800 + n)

    def y_on(self, o):
        return self._force("7", 0x0500 + o)

    def y_off(self, o):
        return self._force("8", 0x0500 + o)

    def link(self, tries=5):
        for _ in range(tries):
            self.ser.reset_input_buffer()
            self.ser.reset_output_buffer()
            time.sleep(0.12)
            self.ser.write(bytes([ENQ]))
            self.ser.flush()
            time.sleep(0.25)
            if ACK in self.ser.read(16):
                return True
        return False

    # ---------- 상태 ----------
    def x(self):
        d = self.read(A_X, 1)
        return None if d is None else {i for i in range(7) if d[0] >> i & 1}

    def y(self):
        d = self.read(A_Y, 2)
        return None if d is None else {i for i in range(14) if d[i // 8] >> (i % 8) & 1}

    def m(self, count=32):
        d = self.read(A_M, count)
        return None if d is None else {i * 8 + b for i, by in enumerate(d)
                                       for b in range(8) if by >> b & 1}

    def d(self, n, words=1):
        b = self.read(A_D + n * 2, words * 2)
        return None if b is None else int.from_bytes(b, "little")

    def set_d(self, n, val, words=1):
        return self.write(A_D + n * 2, val.to_bytes(words * 2, "little", signed=val < 0))

    def estop(self):
        xs = self.x()
        return None if xs is None else (5 in xs)

    def all_off(self, rng=range(1, 31), extra=(40, 120, 21)):
        for _ in range(2):
            for n in list(rng) + list(extra):
                self.m_off(n)
            self.y_off(PUMP)
            self.y_off(VALVE)

    def close(self):
        try:
            self.ser.close()
        except Exception:
            pass

    def report(self):
        s = self.stats
        tot = s["ok"] + s["csum"] + s["noresp"]
        pct = (s["ok"] * 100 // tot) if tot else 0
        return (f"프레임 정상 {s['ok']}/{tot} ({pct}%) "
                f"체크섬오류 {s['csum']} 무응답 {s['noresp']}")


def names(bitset, table):
    return [table.get(i, str(i)) for i in sorted(bitset or [])]
