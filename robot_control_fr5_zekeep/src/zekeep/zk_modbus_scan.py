#!/usr/bin/env python3
"""
zk_modbus_scan.py - ZK3U 의 RS-485 Modbus 포트를 찾는다

배경
  8/5 에 Modbus 를 100건 넘게 시도했지만 **전부 DB9(FX 프로그래밍 포트)** 에서였다.
  매뉴얼(操作说明书 p1~2)에 따르면 Modbus 는 **RS485-1 / RS485-2** 에만 있다.

    출고 통신포맷 3081 = 데이터 8비트 · 무패리티 · 1 정지비트 · 9600 · Modbus
    본기 국번 기본값 = 1
    D8401/D8421 = H10 → Modbus 슬레이브

주소맵 (매뉴얼 p2, ZK3U 소자 ↔ Modbus 주소)
    워드:  D0~D7999      → 0x0000~0x1F3F
           D8000~D8511   → 0x1F40~0x213F     ★ FX 프로토콜로는 못 읽던 영역
    비트:  M0~M7679      → 0x0000~0x1DFF
           Y0~Y377       → 0x3300~0x33FF
           X0~X377       → 0x3400~0x34FF

검증 앵커
    D8001 = 24320  (기종코드 24 + Ver3.20). 이 값이 읽히면 국번·포맷·배선이 전부 맞은 것.

    python3 zk_modbus_scan.py                # 모든 시리얼 포트 자동 탐색
    python3 zk_modbus_scan.py /dev/ttyUSB1   # 특정 포트만
"""
import glob
import sys

from pymodbus.client import ModbusSerialClient

D8001 = 0x1F40 + 1        # = 0x1F41
ANCHOR = 24320

# (보레이트, 데이터비트, 패리티, 정지비트) — 출고값을 맨 앞에
COMBOS = [
    (9600, 8, "N", 1),    # ★ 매뉴얼 출고 설정
    (9600, 8, "E", 1),
    (9600, 8, "O", 1),
    (19200, 8, "N", 1),
    (38400, 8, "N", 1),
    (9600, 8, "N", 2),
    (115200, 8, "N", 1),
]
IDS = [1, 2, 3, 4, 5, 247]


def try_port(port):
    print(f"\n=== {port} ===", flush=True)
    hit = None
    for (baud, bits, par, stop) in COMBOS:
        cli = ModbusSerialClient(port=port, baudrate=baud, bytesize=bits,
                                 parity=par, stopbits=stop, timeout=0.35)
        if not cli.connect():
            print(f"  {baud} {bits}{par}{stop}  포트 열기 실패")
            continue
        found = []
        for sid in IDS:
            try:
                r = cli.read_holding_registers(address=D8001, count=1, device_id=sid)
                if r and not r.isError():
                    v = r.registers[0]
                    mark = "  ★★ D8001 일치!" if v == ANCHOR else ""
                    found.append(f"ID{sid}={v}{mark}")
                    if v == ANCHOR:
                        hit = (port, baud, bits, par, stop, sid)
            except Exception:
                pass
        cli.close()
        print(f"  {baud} {bits}{par}{stop}  →  {', '.join(found) if found else '무응답'}",
              flush=True)
        if hit:
            return hit
    return hit


def dump(port, baud, bits, par, stop, sid):
    print("\n" + "=" * 60)
    print(f"★ 성공  {port}  {baud} {bits}{par}{stop}  국번 {sid}")
    print("=" * 60)
    cli = ModbusSerialClient(port=port, baudrate=baud, bytesize=bits,
                            parity=par, stopbits=stop, timeout=0.5)
    cli.connect()

    def rd(addr, n=1):
        r = cli.read_holding_registers(address=addr, count=n, device_id=sid)
        return None if (not r or r.isError()) else r.registers

    import struct
    print("\n── FX 프로토콜로는 막혀 있던 D8000+ 영역 ──")
    for name, dn in (("D8001 기종", 8001), ("D8002 메모리", 8002),
                     ("D8340 A1 펄스(하)", 8340), ("D8341 A1 펄스(상)", 8341),
                     ("D8350 A2 펄스(하)", 8350), ("D8351 A2 펄스(상)", 8351),
                     ("D8360 A3 펄스(하)", 8360), ("D8361 A3 펄스(상)", 8361)):
        v = rd(0x1F40 + (dn - 8000))
        print(f"  {name:<20} D{dn} = {v[0] if v else '읽기실패'}")

    for j, lo, hi in (("A1", 8340, 8341), ("A2", 8350, 8351), ("A3", 8360, 8361)):
        a = rd(0x1F40 + (lo - 8000), 2)
        if a:
            p = a[0] | (a[1] << 16)
            if p & 0x80000000:
                p -= 1 << 32
            print(f"  → {j} 현재 펄스 = {p}  ({p/177.78:.3f}°)")

    print("\n── 일반 D 영역 (검증용, FX 로 읽던 값과 대조) ──")
    for name, dn in (("D70 총스텝", 70), ("D76 크리프", 76), ("D90 모드", 90)):
        v = rd(dn)
        print(f"  {name:<14} D{dn} = {v[0] if v else '읽기실패'}")
    for j, dn in (("A1", 1010), ("A2", 1030), ("A3", 1050)):
        v = rd(dn, 2)
        if v:
            f = struct.unpack("<f", struct.pack("<HH", v[0], v[1]))[0]
            print(f"  {j} 각도 D{dn} = {f:.3f}°")
    cli.close()


def main():
    ports = sys.argv[1:] or sorted(glob.glob("/dev/ttyUSB*") + glob.glob("/dev/ttyACM*"))
    if not ports:
        sys.exit("시리얼 포트가 없습니다. 어댑터를 꽂았는지 확인하세요.")
    print(f"탐색 대상: {ports}")
    print(f"앵커: D8001(0x{D8001:04X}) == {ANCHOR}")
    for p in ports:
        hit = try_port(p)
        if hit:
            dump(*hit)
            return
    print("\n" + "=" * 60)
    print("전 조합 실패 — 확인할 것")
    print("  · A/B 극성을 바꿔 다시 (망가지지 않습니다)")
    print("  · RS485-1 과 RS485-2 를 각각 시도")
    print("  · HMI 통신설정 화면에서 국번·포맷 확인 (출고값과 다를 수 있음)")
    print("  · 해당 포트가 Modbus 모드인지 (D8400/D8420 설정)")
    print("=" * 60)


if __name__ == "__main__":
    main()
