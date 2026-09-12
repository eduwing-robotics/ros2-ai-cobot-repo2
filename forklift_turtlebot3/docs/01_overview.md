# 01. 프로젝트 개요

Forklift TurtleBot3는 조립식 주택 자동화 공장에서 팔레트를 두 랙과 전달 지점 사이로
운반하는 ROS 2 Jazzy 시스템입니다.

## 목적

- RACK1과 RACK2의 자재를 DROP으로 운반
- DROP의 빈 팔레트를 지정 랙으로 회수
- 각 작업점에서 ArUco 기반 정밀 정렬
- 스테퍼 리프트로 팔레트 적재와 하역
- 작업 종료 후 HOME 정렬과 안전 위치 주차

## 운용 방식

지도 기반 Nav2 대신 HOME 기준 고정 odometry 경로를 사용합니다. 각 작업 위치의 근처까지
odometry로 이동한 뒤 ArUco 마커의 전후·좌우·각도 오차를 사용해 최종 정렬합니다.

리프트는 HEIGHT_2에서 팔레트에 진입하고 HEIGHT_3에서 운반합니다. 하역 후에는
HEIGHT_2에서 20cm 후진합니다. 최종 ReturnHome도 HOME 정렬, HEIGHT_2 주차,
20cm 후진 순서로 종료합니다.

## 연동 대상

- Main Server: ROS 2 Action으로 운반 지시와 완료 결과 교환
- Unity: Pi WebSocket을 통한 직접 수동주행
- Raspberry Pi: TurtleBot bringup, 카메라, 속도 중재기, 리프트 데몬
- PC: 경로주행, ArUco 인식·도킹, 통합 ActionServer

[문서 목록](README.md) · [프로젝트 README](../README.md)
