# 04. 데이터 흐름

## 운반 Action

```text
ExecuteTransport goal
  -> 요청값과 현재 위치 검사
  -> pickup까지 경로 이동
  -> pickup 마커 도킹
  -> HEIGHT_2 확인 / HEIGHT_3 상승
  -> dropoff까지 경로 이동
  -> dropoff 마커 도킹
  -> HEIGHT_2 하강
  -> 20cm 후진
  -> SUCCEEDED result
```

transport_action_server는 각 단계의 명령을 발행한 뒤 대응 상태 토픽의 완료 상태를
확인해야만 다음 단계로 진행합니다. 제한시간, 장치 오류 또는 Action 취소가 발생하면
정지 명령을 보내고 FAILED 또는 CANCELED 결과를 반환합니다.

## HOME 복귀

```text
ReturnHome goal
  -> HOME 경로 이동
  -> ArUco 41 HOME 정렬
  -> HEIGHT_2 주차
  -> 20cm 후진
  -> 현재 위치 HOME_BEHIND
  -> SUCCEEDED result
```

## 카메라와 도킹

camera/image_raw와 camera/camera_info를 aruco_detector가 구독합니다. 검출된 마커 pose는
aruco_docking이 사용하며, 작업점별 calibration.yaml 목표값과 비교해 전진·측면·yaw 오차를
줄입니다.

## 속도 중재

자동 경로와 도킹, Unity 수동입력은 각각 분리된 토픽을 사용합니다. cmd_vel_arbiter가
활성 제어권과 데드맨을 검사해 하나의 /cmd_vel만 TurtleBot에 전달합니다.

## 리프트

```text
LiftCommand -> lift_servo -> UDP 5005 -> fk_jogd.py -> GPIO
LiftState   <- lift_servo <- JSON status <- fk_jogd.py
```

[문서 목록](README.md) · [이전](03_features.md) · [다음](05_validation.md)
