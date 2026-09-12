#!/usr/bin/env python3
"""Battery-free UDP simulator for the fk_jogd.py wire protocol."""

import argparse
import json
import socket


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=15005)
    args = parser.parse_args()

    lead_mm = 5.81
    position = 0.0
    memories = [0.0, 17.0, 30.0, 90.1]
    memory_steps = [0, 11964, 21120, 63488]
    limit_low = 0
    limit_high = 21120
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((args.host, args.port))
    print(f'fake_fk_jogd listening on {args.host}:{args.port}', flush=True)
    try:
        while True:
            raw, peer = sock.recvfrom(4096)
            text = raw.decode('utf-8')
            sequence, command = text.split('|', 1)
            command = command.strip().upper()
            note = 'OK'
            if command == 'Z':
                position = 0.0
                note = 'zero'
            elif command == 'S':
                note = 'stop'
            elif command == '?':
                note = 'status'
            elif command.startswith('J') and command[1:] in ('1', '2', '3', '4'):
                index = int(command[1:]) - 1
                position = memory_steps[index] / 4096.0 * lead_mm
                note = f'{command} arrived'
            elif command.startswith('Y'):
                try:
                    restored_steps = int(float(command[1:]))
                except ValueError:
                    note = f'! invalid restore {command}'
                else:
                    position = restored_steps / 4096.0 * lead_mm
                    note = f'position restored to {restored_steps}'
            elif command.startswith('G'):
                try:
                    target_steps = int(float(command[1:]))
                except ValueError:
                    note = f'! invalid steps {command}'
                else:
                    target_steps = max(limit_low, min(limit_high, target_steps))
                    position = target_steps / 4096.0 * lead_mm
                    note = f'{command} arrived'
            elif command.startswith('P'):
                try:
                    target_mm = float(command[1:])
                except ValueError:
                    note = f'! invalid position {command}'
                else:
                    target_steps = round(target_mm / lead_mm * 4096.0)
                    target_steps = max(limit_low, min(limit_high, target_steps))
                    position = target_steps / 4096.0 * lead_mm
                    note = f'{command} arrived'
            elif command == 'E3':
                memory_steps[2] = round(position / lead_mm * 4096.0)
                memories[2] = position
                note = 'J3 stored'
            elif command == 'W':
                note = 'saved'
            elif command.startswith('L') and ':' in command[1:]:
                try:
                    low_text, high_text = command[1:].split(':', 1)
                    new_low, new_high = int(low_text), int(high_text)
                except ValueError:
                    note = f'! invalid limits {command}'
                else:
                    if new_high <= new_low:
                        note = '! upper limit must exceed lower limit'
                    else:
                        limit_low, limit_high = new_low, new_high
                        note = f'limits {limit_low}~{limit_high}'
            else:
                note = f'! unsupported {command}'
            response = {
                'seq': sequence,
                'mm': position,
                'dir': 0,
                'pos': round(position / lead_mm * 4096.0),
                'lead': lead_mm,
                'full': True,
                'limit_on': True,
                'lo': limit_low,
                'hi': limit_high,
                'mem': memory_steps,
                'mem_mm': memories,
                'note': note,
                'err': '',
            }
            sock.sendto(json.dumps(response).encode('utf-8'), peer)
            print(f'{sequence}|{command} -> {position:.1f}mm', flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        sock.close()


if __name__ == '__main__':
    main()
