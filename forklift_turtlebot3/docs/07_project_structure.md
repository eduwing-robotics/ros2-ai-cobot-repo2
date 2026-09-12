# 07. 프로젝트 구조

```text
forklift_turtlebot3/
├── README.md
├── src/
│   ├── forklift_interfaces/
│   │   ├── action/
│   │   └── msg/
│   └── forklift_control/
│       ├── config/
│       ├── forklift_control/
│       ├── launch/
│       └── tools/
├── hardware/lift/
│   ├── README.md
│   ├── pi/
│   └── pc/
├── scripts/
└── docs/
```

## 포함 파일

- ROS 2 메시지, Action, 노드와 launch 파일
- 작업장 실측 경로·도킹 calibration.yaml
- PC/Pi CycloneDDS 현장 설정
- Pi 스테퍼 데몬과 PC 수동 캘리브레이션 UI
- 배포, 캘리브레이션 검사, 모의시험과 영상 시나리오 스크립트
- Main Server와 Unity 연동 계약

## 제외 파일

- build, install, log와 Python 캐시
- 실행 중 생성되는 Pi의 ~/.forklift_pi.json
- 오래된 리프트 기본값과 이전 높이 사진
- ROS 카메라 bringup으로 대체된 별도 MJPEG 스크립트
- 실물 검증 영상 원본
- 토큰, 비밀번호와 개인 환경 파일

장치별 리프트 스텝값은 기구와 현재 위치에 의존하므로 Pi에서 관리합니다. 반면 작업장
재현에 필요한 경로와 도킹 실측값은 src/forklift_control/config/calibration.yaml로
버전 관리합니다.

[문서 목록](README.md) · [이전](06_project_scope.md) · [프로젝트 README](../README.md)
