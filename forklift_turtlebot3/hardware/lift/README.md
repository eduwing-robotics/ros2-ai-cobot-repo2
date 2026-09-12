# 리프트 설치와 캘리브레이션

28BYJ-48 스테퍼 모터와 ULN2003을 Raspberry Pi GPIO에서 직접 제어합니다.
Pi의 fk_jogd.py가 UDP 5005에서 동작하고, ROS 2 lift_servo 노드와 PC의 fk_key.py가
이 데몬을 사용합니다.

## 배선

| ULN2003 | Raspberry Pi BCM | 물리 핀 |
|:---|:---:|:---:|
| IN1 | GPIO6 | 31 |
| IN2 | GPIO13 | 33 |
| IN3 | GPIO19 | 35 |
| IN4 | GPIO26 | 37 |
| GND | GND | 39 |

외부 5V 전원과 Pi의 GND를 반드시 공통으로 연결합니다. 프로그램 핀 순서는 실제 조립
방향에 맞춘 26,19,13,6입니다.

## Pi 설치

전체 프로젝트의 deploy_to_pi.sh를 사용했다면 /home/pi/forklift에 필요한 파일이 이미
복사됩니다. GPIO 의존성과 권한을 확인합니다.

```bash
bash ~/forklift/setup.sh
~/forklift/jogd_start
```

수동으로 이 폴더만 Pi에 복사한 경우 다음을 실행할 수 있습니다.

```bash
cd <복사한-경로>/hardware/lift/pi
bash install.sh
~/forklift/jogd_start
```

상태와 로그 확인:

```bash
pgrep -af fk_jogd.py
tail -f /tmp/fk_jogd.log
```

jogd_start의 로그 파일은 /tmp/fk_jogd.log입니다.

## PC 수동 UI

PC에서 실행합니다. tkinter를 사용하는 로컬 GUI이며 Pi와 UDP 5005로 직접 통신합니다.

```bash
cd ~/ros2-ai-cobot-repo2/forklift_turtlebot3
python3 hardware/lift/pc/fk_key.py 192.168.20.100
```

- 숫자 2/3: 저장된 J2/J3로 이동
- Ctrl+2/Ctrl+3: 현재 높이를 J2/J3에 저장
- 쉼표/마침표: 현재 위치를 하한/상한으로 지정
- Ctrl+S: 설정 저장

## 현재 공정 기준

| 프리셋 | 논리 이름 | 용도 | 높이 |
|:---:|:---|:---|:---:|
| J2 | HEIGHT_2 | 팔레트 진입·배치·HOME 주차 | 약 17mm |
| J3 | HEIGHT_3 | 팔레트 운반 | 약 35mm |

스텝 수, 리밋, 속도와 프리셋은 Pi의 /home/pi/.forklift_pi.json에 저장됩니다. 이 파일은
기구별 실측값이므로 배포 스크립트와 install.sh가 덮어쓰지 않습니다.

## 기동 시 HEIGHT_2 기준 복구

위치 센서가 없으므로 데몬 재시작 직후 위치는 0으로 표시됩니다. 포크가 실제 HEIGHT_2에
있는 것을 눈으로 확인한 뒤 lift_servo가 실행 중인 상태에서 ZERO를 한 번 보냅니다.
어댑터는 저장된 J2 스텝값을 읽어 현재 위치를 HEIGHT_2로 복구합니다.

```bash
ros2 topic pub --once /forklift/lift/command \
  forklift_interfaces/msg/LiftCommand \
  "{task_id: 'startup-zero', command: 'ZERO'}"
```

실제 높이를 모르는 상태에서는 ZERO를 보내면 안 됩니다. 먼저 수동 UI로 물리 위치와
저장된 프리셋을 확인하세요.

## 안전 특성

- U/D 조그 명령이 0.5초 끊기면 자동 정지
- 설정된 소프트 리밋 밖의 이동 차단
- 정지 후 코일을 꺼 발열 방지
- 풀스텝 사용으로 상승 토크 확보
- 목표 이동 J2/J3는 도착하거나 STOP을 받을 때까지 수행
