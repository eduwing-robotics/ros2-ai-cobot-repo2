# Pick 후보 1건 (v0.3 Final 합의 — AI Perception §4 필드 구성 수용)
#
# ★ candidate_id 는 "단일 응답 내부"에서만 유효하다 (Tracking 미구현 전제 합의).
#   응답 간 지속성을 가정하지 말 것.
# ★ 모든 후보는 같은 capture_stamp 프레임에서 나온다 — 차순위 사용 전
#   재검증 불필요, 단 Freshness(초기 1.0s) 만료 시 E505 규칙으로 재요청.

uint16 candidate_id               # 응답 내 식별자 (0부터, 품질 순)
geometry_msgs/Point position      # frame_id 기준 좌표 [m]
string frame_id                   # 초기 구현: "camera_depth_optical_frame"
                                  #   (Camera->Robot->Workcell TF 변환 주체 = Robot Cell,
                                  #    capture_stamp 시점 TF 사용)
builtin_interfaces/Time capture_stamp   # 측정 프레임 획득 시각 (요청 이후 프레임만)
bool pose_valid                   # 이 후보의 측정 유효성

# --- 방향 (v0.3 Final §2 확정) ---
# yaw_deg: frame_id 프레임 Z축 중심, 오른손 법칙(내려다볼 때 반시계 +), deg
#   0° = 부품 고유축(긴 변)이 frame_id 프레임 X축과 평행할 때
#   범위 -180.0 ~ +180.0, 단 180° 대칭 부품은 -90 ~ +90 으로 정규화
float32 yaw_deg
bool yaw_valid
string orientation_source         # 방향 추정 근거 — 허용값(8/11 비전 확정):
                                  #   "obb"|"mask_pca"|"keypoints"|"template"|"none"
                                  #   미구현/판정불가 = yaw_valid=false + "none"

float32 quality                   # 품질 점수 0.0~1.0 — 비전이 정렬 기준으로 사용
                                  #   (M8 합의: 최종 후보 선택=Robot Cell, 품질 정렬=Vision)
