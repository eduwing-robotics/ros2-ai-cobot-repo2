# Server / FMS 문제 해결 및 검증

이 문서는 기능 목록이 아니라 현재 source와 test에서 확인되는 통합 설계 변경 및 검증 범위를 기록합니다. 실제 장비 E2E를 수행하지 않은 항목을 hardware PASS로 표현하지 않습니다.

## 1. TurtleBot 좌표 authority 분리

**문제**
Server/FMS와 TurtleBot이 모두 pickup/dropoff의 physical pose를 소유하면 map·marker 변경 시 양쪽 구현이 강하게 결합될 수 있습니다.

**원인 분석**
Material Delivery는 supply group과 destination이라는 업무 의미를 가지며, navigation pose는 TurtleBot runtime의 물리 환경 책임입니다.

**개선**
`TransportLocationResolver`와 `ForkliftActionAdapter`가 `pickup_code`, `dropoff_code`만 Action Goal에 전달하도록 했습니다. 현재 policy는 `RACK1`/`RACK2`/`DROP` logical code를 사용합니다.

**결과 / 현재 상태**
FMS는 Delivery lifecycle과 logical source/destination을 authority로 유지하고, TurtleBot은 navigation pose·movement를 소유합니다.

**검증**
logical location resolver, Forklift Action adapter, ROS2 generated-goal field regression test가 있습니다.

## 2. PRE_ROOF bundled result에서 per-view lifecycle으로 전환

**문제**
하나의 5-view final result만 받는 구조는 현재 view의 ACK, 실패 view 재검사, out-of-order/duplicate result를 durable하게 표현하기 어렵습니다.

**원인 분석**
PRE_ROOF는 TOP·LEFT·RIGHT·FRONT·BEHIND 순서를 서버가 제어해야 하며, UDP packet 순서 자체를 lifecycle authority로 삼을 수 없습니다.

**개선**
`ProductionInspectionViewRequest`에 request id, cycle, view, status, digest, ACK/retry/result evidence를 저장하고, `ProductionCompletionService`와 `PreRoofUdpRuntime`이 current view 결과 뒤에 다음 request를 materialize합니다.

**결과 / 현재 상태**
5/5 PASS에서만 Roof gate를 release하고, FAIL은 동일 view 재검사로 이어집니다. `production_valid`는 provenance이며 gate 조건이 아닙니다. v0.1 bundled packet은 v0.2 runtime에서 unsupported로 처리됩니다.

**검증**
per-view ACK/result, duplicate·stale·out-of-order result, reinspection, Unity inspection projection, loopback rehearsal 관련 test/script가 있습니다.

## 3. Production stage authority 분산

**문제**
Unity, Voice 상태 조회, 자동 TTS가 JobStep·Delivery·inspection을 각각 해석하면 같은 Job을 다른 공정으로 표시할 수 있습니다.

**원인 분석**
수입검사, empty pallet return, PRE_ROOF, HOUSE_OUTBOUND는 단일 runnable JobStep만으로 현재 상위 공정을 설명하기 어렵습니다.

**개선**
`UnityCurrentStageProjectionService`가 durable state에서 canonical 12-stage PROCESS projection을 계산합니다.

**결과 / 현재 상태**
Unity realtime, Voice `QUERY_JOB_STATUS`, automatic TTS가 `process_stage_code`와 display projection을 공통 기준으로 사용합니다.

**검증**
production snapshot/realtime, Voice status, production event TTS regression test가 동일 projection을 확인합니다.

## 4. Restart 후 중복 physical command 위험

**문제**
서버 restart 이후 RUNNING 또는 active ExecutionAttempt를 PENDING처럼 재전송하면 실제 Robot Cell/TurtleBot이 이미 수행한 물리 동작을 중복 실행할 수 있습니다.

**원인 분석**
process memory만으로는 crash 직전 external command가 실제로 수행됐는지 판단할 수 없습니다.

**개선**
JobStep과 ExecutionAttempt의 dispatch/accept/result evidence를 PostgreSQL에 기록합니다. FMS Worker는 fresh process에서 RUNNING Step의 terminal evidence만 reconcile하고, ambiguous active command를 자동 replay하지 않습니다.

**결과 / 현재 상태**
SUCCEEDED/COMPLETED 이력은 보존하며 재실행하지 않습니다. 불확실한 active work는 manual recovery boundary로 남습니다.

**검증**
FMS main loop, transport attempt durability, execution coordinator, restart/recovery regression test가 있습니다.

## 5. Terminal Job의 stranded DROP recovery

**문제**
terminal/canceled Job의 forward transport는 성공했지만 empty pallet return evidence가 없으면 DROP ownership이 남아 후속 Delivery가 차단될 수 있습니다.

**원인 분석**
Job cancel은 physical pallet의 제거 증거가 아니며 successful forward transport history를 삭제하거나 DROP flag만 FREE로 바꾸는 것은 안전하지 않습니다.

**개선**
`TerminalDropRecoveryService`는 benchmark database boundary를 caller가 별도로 보장하는 bounded recovery primitive입니다. API에서는 `TEST_OVERRIDE_ENABLED`와 `smart_factory_benchmark` database bind를 확인하는 guarded Test Override cleanup route를 통해 호출하며, 조건을 잠그고 확인한 뒤 기존 lifecycle 형식의 synthetic `EXECUTE_TRANSPORT_EMPTY_RETURN` SUCCEEDED Attempt를 기록합니다. 이 경로는 TurtleBot adapter를 호출하지 않습니다.

**결과 / 현재 상태**
forward transport history는 보존되고, 해당 return evidence가 DROP release의 durable 근거가 됩니다. 이미 성공한 return 또는 active attempt가 있으면 recovery를 허용하지 않아 중복 생성을 방지합니다.

**검증**
terminal DROP recovery, empty return, DROP resource regression test가 idempotency와 ownership 조건을 다룹니다.

## 6. ROOF 완료와 Production 완료의 분리

**문제**
ROOF JobStep 성공 직후 Job을 COMPLETED로 전환하면 완성 주택 운반을 PROCESS 상에서 표현할 durable checkpoint가 없습니다.

**원인 분석**
현재 Recipe의 canonical roof step은 PRE_ROOF-gated terminal step이지만, Unity PROCESS에는 `HOUSE_OUTBOUND`가 별도 단계로 필요합니다.

**개선**
선택된 PRE_ROOF-gated ROOF step은 완료 후 즉시 generic terminal completion으로 닫지 않고, ROOF completed + Job non-terminal durable 조합을 `HOUSE_OUTBOUND`로 projection합니다. Test Override의 explicit completion은 기존 `ProductionOrchestrationService.complete_job()`을 사용합니다.

**결과 / 현재 상태**
새 DB column 없이 restart 후에도 roof completion evidence로 Stage 11을 다시 계산할 수 있고, explicit completion 뒤에만 `COMPLETED`가 됩니다.

**검증**
production completion lifecycle, PROCESS projection, Test Override/Production Monitor regression test가 roof 재-dispatch 방지와 outbound completion을 다룹니다.

## 검증 전략과 범위

| 영역 | 저장소에서 확인 가능한 검증 |
| --- | --- |
| Domain / lifecycle | unit·service·SQLite fixture test, Recipe/JobStep/Delivery/Inspection state transition |
| API / realtime | FastAPI API test, Unity WebSocket/snapshot/projection test |
| FMS | fake Robot Cell·fake Forklift adapter, FMS worker/dispatch/recovery regression |
| Vision | validated schema, fake/loopback UDP runtime, PRE_ROOF rehearsal helper |
| PostgreSQL / Redis | opt-in `postgres_integration`, `redis_integration` marker 및 target-safety test |
| Scripts | guarded seed, cleanup, rehearsal, fake production demo helper |

Fake transport, fixture, loopback rehearsal은 실제 FR5, ZeKeep, TurtleBot, Vision hardware를 운전한 결과가 아닙니다. 실제 장비·운영 DB 환경은 별도 설정과 승인된 E2E 절차가 필요합니다.
