using System.Collections;
using UnityEngine;

public class ModularHouseLocalIntegrationTest : MonoBehaviour
{
    private static readonly string[] Fr5JointNames =
    {
        "joint1",
        "joint2",
        "joint3",
        "joint4",
        "joint5",
        "joint6"
    };

    // 눈으로도 무리 없이 확인할 수 있는 작은 테스트 자세입니다.
    private static readonly float[] Fr5TestPoseRadians =
    {
        0.15f,
        -0.25f,
        0.35f,
        -0.20f,
        0.25f,
        0.10f
    };

    private static readonly string[] ZkJointNames =
    {
        "a1_joint",
        "a2_joint",
        "a3_joint"
    };

    private static readonly float[] ZkTestPoseRadians =
    {
        0.12f,
        -0.18f,
        0.24f
    };

    public void Run(FactoryStateManager stateManager)
    {
        StartCoroutine(RunRoutine(stateManager));
    }

    private IEnumerator RunRoutine(
        FactoryStateManager stateManager)
    {
        if (stateManager == null)
        {
            Debug.LogError(
                "[LOCAL SELF TEST FAIL] FactoryStateManager를 찾지 못했습니다.");
            Destroy(gameObject);
            yield break;
        }

        PageManager pageManager =
            FindAnyObjectByType<PageManager>();

        if (pageManager != null && pageManager.taskButton != null)
        {
            pageManager.taskButton.onClick.Invoke();
            yield return null;
        }

        ZKJointStateApplier fr5 = FindRobotApplier("fr5");

        if (fr5 == null)
        {
            Debug.LogError(
                "[LOCAL SELF TEST FAIL] robotId=fr5 자세 적용기를 찾지 못했습니다.");
            Destroy(gameObject);
            yield break;
        }

        // 실제 서버 순서와 동일하게 현재 생산 공정을 먼저 전달합니다.
        // 이번 Transport는 4번 '외벽 팔레트 운반'에 해당합니다.
        stateManager.ApplyLocalProductionStatusForTest(
            new ProductionJobData
            {
                job_id = "LOCAL-JOB",
                job_code = "LOCAL-TEST-JOB",
                product_code = "HOUSE_B",
                product = "HOUSE_B",
                // 최종 계약: HOUSE_B는 지붕2를 사용합니다.
                roof_option_code = "ROOF_02",
                status = "RUNNING",
                job_status = "RUNNING",
                current_step_id = "LOCAL-STEP-04",
                current_operation = "DELIVER_OUTER_WALL_PALLET",
                current_step_status = "RUNNING",
                ready = true,
                progress = 0.3f
            });

        yield return null;

        bool hasOriginalFr5Pose = fr5.TryGetCurrentJointPositions(
            Fr5JointNames,
            out float[] originalFr5Pose);

        stateManager.ApplyLocalJointStateForTest(
            new RobotJointStateData
            {
                robot_id = "fr5",
                joint_names = Fr5JointNames,
                positions = Fr5TestPoseRadians,
                velocities = new float[0],
                efforts = new float[0]
            });

        // 메시지 이벤트와 관절 적용은 동기식이다. 물리 프레임을 기다리면
        // 주변 물체와의 충돌로 관절이 밀려 통신 적용 테스트가 오탐한다.
        float jointError = fr5.GetMaxJointErrorDegrees(
            Fr5JointNames,
            Fr5TestPoseRadians);
        bool fr5Passed = hasOriginalFr5Pose && jointError <= 0.5f;

        if (hasOriginalFr5Pose)
        {
            fr5.ApplyJointState(Fr5JointNames, originalFr5Pose);
        }

        float originalGripperOpening = fr5.CurrentGripperOpening01;
        fr5.ApplyJointState(
            new[] { "gripper_joint" },
            new[] { 0f });
        bool gripperClosed = fr5.HasVisualGripper &&
            fr5.CurrentGripperOpening01 <= 0.01f;
        fr5.ApplyJointState(
            new[] { "gripper_joint" },
            new[] { 1f });
        bool gripperOpened = fr5.CurrentGripperOpening01 >= 0.99f;
        bool gripperPassed = gripperClosed && gripperOpened;
        fr5.SetGripperOpening(originalGripperOpening);

        ZKJointStateApplier zk = FindRobotApplier("zkbot2");
        bool zkPassed = zk != null;
        float zkJointError = float.PositiveInfinity;

        if (zk != null)
        {
            bool hasOriginalZkPose = zk.TryGetCurrentJointPositions(
                ZkJointNames,
                out float[] originalZkPose);

            stateManager.ApplyLocalJointStateForTest(
                new RobotJointStateData
                {
                    robot_id = "zkbot2",
                    joint_names = ZkJointNames,
                    positions = ZkTestPoseRadians,
                    velocities = new float[0],
                    efforts = new float[0]
                });

            zkJointError = zk.GetMaxJointErrorDegrees(
                ZkJointNames,
                ZkTestPoseRadians);
            zkPassed = hasOriginalZkPose && zkJointError <= 0.5f;

            if (hasOriginalZkPose)
            {
                zk.ApplyJointState(ZkJointNames, originalZkPose);
            }
        }

        // 두 로봇 모두 원래 자세로 복구한 후에만 물리 업데이트를 허용한다.
        yield return null;

        stateManager.ApplyLocalTransportStatusForTest(
            new TransportStatusData
            {
                req_id = "LOCAL-TRANSPORT-TEST",
                job_id = "LOCAL-JOB",
                delivery_id = "LOCAL-DELIVERY",
                robot_id = "forklift_01",
                task_type = "EXECUTE_TRANSPORT",
                phase = "DELIVERING_DROPOFF",
                progress = 0.5f,
                result = null,
                error_code = null,
                detail = "Unity 로컬 통합 테스트"
            });

        yield return null;

        ProductionProcessUI processUI =
            FindAnyObjectByType<ProductionProcessUI>();
        bool transportPassed =
            processUI != null &&
            processUI.IsTransportProgressDisplayed(0.5f);
        bool processStepPassed =
            processUI != null &&
            processUI.IsProcessStepCurrent(4);

        ForkliftPoseApplier forklift =
            FindAnyObjectByType<ForkliftPoseApplier>();
        bool forkliftPosePassed = false;

        if (forklift != null)
        {
            stateManager.ApplyLocalMobileRobotPoseForTest(
                new MobileRobotPoseData
                {
                    robot_id = "forklift_01",
                    frame_id = "map",
                    position = new PositionData
                    {
                        x = 0f,
                        y = 0f,
                        z = 0f
                    },
                    orientation = new QuaternionData
                    {
                        x = 0f,
                        y = 0f,
                        z = 0f,
                        w = 1f
                    }
                });
            yield return null;
            forkliftPosePassed = Vector3.Distance(
                forklift.DisplayedPosition,
                forklift.SceneStartPosition) <= 0.001f;
        }

        FactoryMaterialFlowController materialFlow =
            FindAnyObjectByType<FactoryMaterialFlowController>();
        string materialFlowReport =
            "FactoryMaterialFlowController 없음";
        bool materialFlowPassed = materialFlow != null &&
            materialFlow.RunMaterialFlowSelfTest(
                out materialFlowReport);

        bool preRoofPassed = false;

        if (pageManager != null && pageManager.inspectionButton != null)
        {
            pageManager.inspectionButton.onClick.Invoke();
            yield return null;

            InspectionPageUI inspectionUI =
                FindAnyObjectByType<InspectionPageUI>();

            if (inspectionUI != null)
            {
                ProductionInspectionStatusData preRoof =
                    CreatePreRoofTestStatus(
                        "RUNNING",
                        null,
                        "NOT_RELEASED");
                preRoof.inspection.current_view = "LEFT";
                preRoof.inspection.views = CreateRunningViews("LEFT");
                stateManager.ApplyLocalProductionInspectionForTest(preRoof);
                yield return null;
                bool runningPassed =
                    inspectionUI.AssemblyFeedState ==
                    InspectionPageUI.FeedState.Inspecting &&
                    inspectionUI.CurrentAssemblyView == "LEFT";

                preRoof.inspection.status = "COMPLETED";
                preRoof.inspection.result = "PASS";
                preRoof.inspection.current_view = null;
                preRoof.inspection.gate_state = "NOT_RELEASED";
                preRoof.inspection.views = CreatePassingViews();
                stateManager.ApplyLocalProductionInspectionForTest(preRoof);
                yield return null;
                bool gateWaitingPassed =
                    inspectionUI.AssemblyFeedState ==
                    InspectionPageUI.FeedState.GateWaiting;

                preRoof.inspection.gate_state = "RELEASED";
                stateManager.ApplyLocalProductionInspectionForTest(preRoof);
                yield return null;
                bool releasedPassed =
                    inspectionUI.AssemblyFeedState ==
                    InspectionPageUI.FeedState.Passed;

                preRoofPassed = runningPassed &&
                    gateWaitingPassed && releasedPassed;
            }

            if (pageManager.taskButton != null)
            {
                pageManager.taskButton.onClick.Invoke();
                yield return null;
            }
        }

        // 로컬용 큰 Job ID/Gate 상태가 이후 실제 서버 시험에 남지 않게 합니다.
        materialFlow?.ResetMaterialFlow();

        if (fr5Passed && gripperPassed && zkPassed &&
            transportPassed && processStepPassed &&
            forkliftPosePassed && materialFlowPassed && preRoofPassed)
        {
            Debug.Log(
                $"[LOCAL SELF TEST PASS] FR5 오차={jointError:F3}°, " +
                "FR5 그리퍼=정상, " +
                $"ZK 오차={zkJointError:F3}°, " +
                "현재 공정=4번, Transport 진행 바=50%, " +
                "Forklift HOME 원점=정상, " +
                $"자재 흐름=({materialFlowReport}), " +
                "PRE_ROOF Gate 계약=정상. " +
                "이제 실제 WebSocket 연결 시험이 가능합니다.");
        }
        else
        {
            Debug.LogError(
                $"[LOCAL SELF TEST FAIL] FR5={fr5Passed} " +
                $"(오차={jointError:F3}°), FR5 그리퍼={gripperPassed}, " +
                $"ZK={zkPassed} " +
                $"(오차={zkJointError:F3}°), 현재 공정={processStepPassed}, " +
                $"Transport={transportPassed}, " +
                $"Forklift HOME 원점={forkliftPosePassed}, " +
                $"자재 흐름={materialFlowPassed} ({materialFlowReport}), " +
                $"PRE_ROOF Gate={preRoofPassed}");
        }

        Destroy(gameObject);
    }

    private static ZKJointStateApplier FindRobotApplier(
        string robotId)
    {
        ZKJointStateApplier[] appliers =
            FindObjectsByType<ZKJointStateApplier>(
                FindObjectsInactive.Include);

        foreach (ZKJointStateApplier applier in appliers)
        {
            if (applier != null && applier.RobotId == robotId)
            {
                return applier;
            }
        }

        return null;
    }

    private static ProductionInspectionStatusData CreatePreRoofTestStatus(
        string status,
        string result,
        string gateState)
    {
        return new ProductionInspectionStatusData
        {
            job_id = 900001,
            inspection_type = "PRE_ROOF",
            inspection = new ProductionInspectionData
            {
                inspection_id = 900001,
                inspection_request_id = "LOCAL-PRE-ROOF-TEST",
                inspection_cycle = 1,
                status = status,
                result = result,
                vision_production_valid = false,
                production_valid = false,
                gate_state = gateState,
                views = new ProductionInspectionViewData[0],
                runtime_profile = "PRE_ROOF_5VIEW"
            }
        };
    }

    private static ProductionInspectionViewData[] CreatePassingViews()
    {
        string[] names = { "TOP", "LEFT", "RIGHT", "FRONT", "BEHIND" };
        ProductionInspectionViewData[] views =
            new ProductionInspectionViewData[names.Length];

        for (int i = 0; i < names.Length; i++)
        {
                views[i] = new ProductionInspectionViewData
                {
                    view_name = names[i],
                    status = "PASS",
                    result = "PASS",
                    reason_code = null
                };
        }

        return views;
    }

    private static ProductionInspectionViewData[] CreateRunningViews(
        string currentView)
    {
        string[] names = { "TOP", "LEFT", "RIGHT", "FRONT", "BEHIND" };
        var views = new ProductionInspectionViewData[names.Length];
        bool reachedCurrent = false;

        for (int i = 0; i < names.Length; i++)
        {
            string state;
            if (names[i] == currentView)
            {
                state = "IN_PROGRESS";
                reachedCurrent = true;
            }
            else
            {
                state = reachedCurrent ? "PENDING" : "PASS";
            }

            views[i] = new ProductionInspectionViewData
            {
                view_name = names[i],
                status = state,
                result = state == "PASS" ? "PASS" : null,
                reason_code = null
            };
        }

        return views;
    }
}
