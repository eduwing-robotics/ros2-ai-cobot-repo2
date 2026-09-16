ZKBot301ED 색상 영역 분리 STL 패키지

이 패키지는 OBJ를 Visual로 사용하지 않습니다.
사용자가 OBJ에서 지정한 흰색/검정색 face 그룹을 링크별 STL 두 개로 분리했습니다.

- Visual: *_white.stl + *_black.stl
- Collision: 결합이 정상인 원본 *.stl
- 모든 Visual과 Collision origin: 0 0 0
- 관절 위치/축/제한: 원본 URDF 그대로

Unity 적용:
1. 기존 ZKBot 오브젝트와 Assets의 기존 ZKBot 폴더를 삭제합니다.
2. ZKBot301ED_UserColored_SplitSTL 폴더 전체를 Assets에 넣습니다.
3. zkbot301ed_colored_split_stl_with_suction.urdf를 우클릭합니다.
4. Import Robot from URDF를 실행합니다.
