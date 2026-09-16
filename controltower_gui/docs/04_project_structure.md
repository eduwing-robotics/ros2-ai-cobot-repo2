# 파일 구성과 포함 기준

## 포함

- `Assets/`: 스크립트, 씬, 모델, 텍스처, 설정 에셋 및 모든 `.meta`
- `Packages/manifest.json`, `Packages/packages-lock.json`
- `ProjectSettings/`
- README와 docs

씬과 프리팹은 GUID로 참조하므로 폴더를 임의 재배치하거나 .meta를 삭제하지 않습니다. Unity 기본 구조를 유지하고 문서만 팀 저장소 형식에 맞췄습니다. 모델·이미지의 출처와 이용 조건은 배포 전 제공자가 확인해야 하며 새 라이선스를 임의 부여하지 않습니다.

## 제외

`Library`, `Temp`, `Logs`, `obj`, `UserSettings`, 빌드 결과, IDE 자동 생성 파일, 촬영 영상, 작업 백업과 발표자료는 포함하지 않습니다.

Unity 실행 에셋과 .meta를 포함하고, .gitignore로 캐시와 자동 생성 파일을 제외합니다. 실행용 원본 에셋에 촬영 영상이나 비밀키를 추가하지 않습니다.

## 저장소 배치

`controltower_gui/`가 Unity 프로젝트 루트입니다. Unity Hub에는 이 폴더를 추가합니다. 상위 저장소에는 다른 팀 모듈이 함께 존재할 수 있으며, Unity 파일을 상위로 이동할 필요가 없습니다.
