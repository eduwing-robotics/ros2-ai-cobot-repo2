# 05. 검증

## 정적·모의 검증

- 두 ROS 2 패키지 colcon build 성공
- Python console executable과 launch 인자 확인
- Bash 배포·검증 스크립트 문법 검사
- 격리된 ROS_DOMAIN_ID=98에서 모의 운반 4회와 최종 HOME 복귀 성공

모의 검증 명령:

```bash
cd ~/ros2-ai-cobot-repo2/forklift_turtlebot3
source /opt/ros/jazzy/setup.bash
colcon build --base-paths src --symlink-install
source install/setup.bash
./scripts/mock_action_test.sh
```

## 실물 검증

2026-09-12 실물 영상 시나리오 3개를 모두 성공했습니다.

| 시나리오 | 순서 | 결과 |
|:---:|:---|:---:|
| 1 | RACK1 -> DROP | PASS |
| 2 | DROP -> RACK1 -> RACK2 -> DROP | PASS |
| 3 | DROP -> RACK2 -> HOME 정렬 -> HEIGHT_2 -> 20cm 후진 | PASS |

영상 파일은 저장소에 포함하지 않았습니다. 이 표는 현장 실행 결과를 기록한 것이며,
재실행 시에는 scripts/run_video_scenario.sh를 사용합니다.

## 현장 확인이 필요한 조건

- Pi의 리프트 프리셋 J2/J3와 물리 높이 일치
- PC/Pi/Main Server의 ROS_DOMAIN_ID와 DDS peer 일치
- 카메라 image_raw와 camera_info 수신
- odom과 scan 수신
- 작업장 배치 변경 시 calibration.yaml 재측정

[문서 목록](README.md) · [이전](04_data_flow.md) · [다음](06_project_scope.md)
