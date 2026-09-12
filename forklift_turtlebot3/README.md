# Forklift TurtleBot3

ROS 2 Jazzy 기반 TurtleBot3 자재 운반 로봇입니다. 고정 HOME 기준 odometry 경로로
작업 지점까지 이동하고, ArUco 마커로 RACK1·RACK2·DROP·HOME에서 정밀 정렬합니다.
28BYJ-48 스테퍼 리프트는 Raspberry Pi의 UDP 데몬을 통해 제어합니다.

## 담당 범위

- RACK1/RACK2와 DROP 사이 자동 운반
- ArUco 기반 접근·도킹·20cm 후진
- HEIGHT_2 팔레트 진입/배치, HEIGHT_3 운반
- 최종 HOME 정렬, HEIGHT_2 주차, 20cm 후진
- Main Server ROS 2 Action 연동
- Unity에서 Pi WebSocket으로 직접 수동주행

SLAM·Nav2 지도 주행은 사용하지 않습니다. 좌표, 경유점, 마커 ID와 도킹 절차는
TurtleBot 측 설정에서 관리합니다.

## 확정 작업 위치

| 코드 | 역할 | ArUco ID |
|:---|:---|:---:|
| HOME | 시작·복귀 기준 | 41 |
| RACK1 | Point A 팔레트 | 41 |
| RACK2 | Point B 팔레트 | 40 |
| DROP | 전달·빈 팔레트 회수 | 42 |

정상 공정 순서는 다음과 같습니다.

```text
RACK1 -> DROP
DROP  -> RACK1
RACK2 -> DROP
DROP  -> RACK2
ReturnHome
```

모든 운반은 HEIGHT_2에서 포크를 넣고 HEIGHT_3(실측 약 35mm)로 운반한 뒤,
HEIGHT_2에서 내려놓고 20cm 후진합니다. DROP에서도 별도의 추가 하강은 하지 않습니다.

## 디렉터리

```text
forklift_turtlebot3/
├── src/
│   ├── forklift_interfaces/       ROS 2 메시지와 Action
│   └── forklift_control/          주행·도킹·리프트·통합 Action
├── hardware/lift/
│   ├── pi/                        GPIO 리프트 UDP 데몬
│   └── pc/                        리프트 수동 설정 UI
├── scripts/                       배포·검증·영상 시나리오
└── docs/                          설계와 연동 문서
```

## PC 빌드

저장소 루트에서 다음과 같이 빌드합니다.

```bash
cd ~/ros2-ai-cobot-repo2/forklift_turtlebot3
source /opt/ros/jazzy/setup.bash
colcon build \
  --base-paths src \
  --packages-select forklift_interfaces forklift_control \
  --symlink-install
source install/setup.bash
```

## Pi 배포

PC에서 실행합니다. 기본 Pi 주소는 192.168.20.100이며 인자로 바꿀 수 있습니다.

```bash
cd ~/ros2-ai-cobot-repo2/forklift_turtlebot3
./scripts/deploy_to_pi.sh pi@192.168.20.100
```

이 스크립트는 ROS 패키지, Pi DDS 설정과 리프트 데몬을 배포한 뒤 Pi 워크스페이스를
빌드합니다. 기존 Pi의 /home/pi/.forklift_pi.json 캘리브레이션은 덮어쓰지 않습니다.

## Pi 실행 순서

아래 각 항목은 Pi의 별도 터미널에서 실행합니다.

처음 한 번 WebSocket 의존성을 설치합니다. GPIO 의존성은 아래 setup.sh가 확인합니다.

```bash
sudo apt install -y python3-websockets
```

### 1. 리프트 UDP 데몬

처음 설치한 Pi라면 먼저 GPIO 환경을 확인하고 J2/J3를 캘리브레이션합니다.
자세한 절차는 [리프트 문서](hardware/lift/README.md)를 참고하세요.

```bash
bash ~/forklift/setup.sh
```

설정이 끝났으면 데몬을 시작합니다.

```bash
~/forklift/jogd_start
```

### 2. TurtleBot3 bringup

```bash
source /opt/ros/jazzy/setup.bash
source ~/turtlebot3_ws/install/setup.bash
export TURTLEBOT3_MODEL=burger
export ROS_DOMAIN_ID=73
export RMW_IMPLEMENTATION=rmw_cyclonedds_cpp
export CYCLONEDDS_URI=file:///home/pi/cyclonedds_unicast.xml
ros2 launch turtlebot3_bringup robot.launch.py
```

### 3. 카메라

```bash
source /opt/ros/jazzy/setup.bash
source ~/turtlebot3_ws/install/setup.bash
export ROS_DOMAIN_ID=73
export RMW_IMPLEMENTATION=rmw_cyclonedds_cpp
export CYCLONEDDS_URI=file:///home/pi/cyclonedds_unicast.xml
ros2 launch turtlebot3_bringup camera.launch.py
```

### 4. 리프트 ROS 어댑터

```bash
source /opt/ros/jazzy/setup.bash
source ~/forklift_ws/install/setup.bash
export ROS_DOMAIN_ID=73
export RMW_IMPLEMENTATION=rmw_cyclonedds_cpp
export CYCLONEDDS_URI=file:///home/pi/cyclonedds_unicast.xml
ros2 run forklift_control lift_servo
```

리프트가 실제 HEIGHT_2에 있는 것을 확인한 다음, 다른 Pi 터미널에서 기동할 때마다
다음 명령을 한 번 보냅니다. 위치가 확실하지 않다면 보내지 마세요.

```bash
source /opt/ros/jazzy/setup.bash
source ~/forklift_ws/install/setup.bash
export ROS_DOMAIN_ID=73
export RMW_IMPLEMENTATION=rmw_cyclonedds_cpp
export CYCLONEDDS_URI=file:///home/pi/cyclonedds_unicast.xml
ros2 topic pub --once /forklift/lift/command \
  forklift_interfaces/msg/LiftCommand \
  "{task_id: 'startup-zero', command: 'ZERO'}"
```

### 5. 속도 중재기와 Unity WebSocket

Unity 제어 토큰은 사용하지 않습니다. 같은 로컬 네트워크에서만 사용하고 TCP 8765를
인터넷이나 공유기 포트포워딩으로 노출하지 마세요.

```bash
source /opt/ros/jazzy/setup.bash
source ~/forklift_ws/install/setup.bash
export ROS_DOMAIN_ID=73
export RMW_IMPLEMENTATION=rmw_cyclonedds_cpp
export CYCLONEDDS_URI=file:///home/pi/cyclonedds_unicast.xml
ros2 launch forklift_control forklift_pi.launch.py
```

이 launch는 자동주행 속도도 실제 /cmd_vel로 전달하므로 Unity를 사용하지 않는 날에도
실행해야 합니다.

## PC 통합 노드 실행

PC DDS XML의 인터페이스 이름과 peer IP는 현장 네트워크에 맞아야 합니다. 현재 제공된
설정은 PC 192.168.20.40, Pi 192.168.20.100, Main Server 192.168.20.20 기준입니다.

```bash
cd ~/ros2-ai-cobot-repo2/forklift_turtlebot3
source /opt/ros/jazzy/setup.bash
source install/setup.bash
export ROS_DOMAIN_ID=73
export RMW_IMPLEMENTATION=rmw_cyclonedds_cpp
unset ROS_STATIC_PEERS
DDS_PC_PATH="$(ros2 pkg prefix --share forklift_control)/config/cyclonedds_pc_unicast.xml"
export CYCLONEDDS_URI="file://$DDS_PC_PATH"
ros2 launch forklift_control forklift.launch.py \
  enable_action_server:=true \
  use_mock_lift:=false
```

## 실행 확인

```bash
ros2 node list
ros2 action list
ros2 topic info /forklift/route/command
ros2 topic info /forklift/dock/command
timeout 10 ros2 topic echo /odom --once
timeout 10 ros2 topic echo /scan --once
timeout 10 ros2 topic echo /camera/image_raw --once
```

## Action 예시

RACK1에서 DROP으로 운반:

```bash
ros2 action send_goal /forklift/execute_transport \
  forklift_interfaces/action/ExecuteTransport \
  "{req_id: 'mvp-001', job_id: 1, delivery_id: 1, pickup_code: 'RACK1', dropoff_code: 'DROP'}" \
  --feedback
```

최종 HOME 복귀:

```bash
ros2 action send_goal /forklift/return_home \
  forklift_interfaces/action/ReturnHome \
  "{req_id: 'mvp-home-001'}" \
  --feedback
```

## 검증 스크립트

```bash
cd ~/ros2-ai-cobot-repo2/forklift_turtlebot3
./scripts/check_calibration.sh
./scripts/mock_action_test.sh
./scripts/run_video_scenario.sh 1
./scripts/run_video_scenario.sh 2
./scripts/run_video_scenario.sh 3
```

영상 시나리오는 실제 로봇 노드가 모두 실행된 상태에서 사용합니다. 모의 시험은 별도의
ROS domain 98에서 네 번의 운반과 최종 HOME 복귀를 검사합니다.

## 캘리브레이션

- 도킹·경로 실측값: src/forklift_control/config/calibration.yaml
- 범용 제어값: src/forklift_control/config/forklift_params.yaml
- Pi 리프트 프리셋: /home/pi/.forklift_pi.json

현재 리프트의 확정 논리 높이는 J2=HEIGHT_2 약 17mm, J3=HEIGHT_3 약 35mm입니다.
Pi JSON은 실제 장치에서 조정되는 런타임 파일이므로 저장소의 예전 값으로 덮어쓰지 않습니다.

## 문서

- [상세 문서 목록](docs/README.md)
- [Main Server 연동](docs/main_server_integration.md)
- [Unity 직접 수동주행](docs/unity_manual_websocket.md)
- [리프트 설치와 캘리브레이션](hardware/lift/README.md)
