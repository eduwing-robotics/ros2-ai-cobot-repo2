using System;
using System.Collections.Generic;
using Unity.Robotics.ROSTCPConnector;
using RosMessageTypes.Sensor;
using UnityEngine;

public class ZKBot301EDJointStateSubscriber : MonoBehaviour
{
    public string topicName = "/zekeep/joint_states";

    public ArticulationBody a1Joint;
    public ArticulationBody a2Joint;
    public ArticulationBody a3Joint;

    private readonly Dictionary<string, ArticulationBody> joints = new();

    void Start()
    {
        RegisterJoint("a1_joint", a1Joint);
        RegisterJoint("a2_joint", a2Joint);
        RegisterJoint("a3_joint", a3Joint);

        ROSConnection.GetOrCreateInstance()
            .Subscribe<JointStateMsg>(topicName, OnJointState);

        Debug.Log($"[ZKeep] ROS 구독 시작: {topicName}");
    }

    void RegisterJoint(string jointName, ArticulationBody joint)
    {
        if (joint == null)
        {
            Debug.LogError(
                $"[ZKeep] {jointName}의 ArticulationBody가 Inspector에 연결되지 않았습니다."
            );
            return;
        }

        if (joint.jointType != ArticulationJointType.RevoluteJoint)
        {
            Debug.LogWarning(
                $"[ZKeep] {jointName}의 Joint Type이 Revolute가 아닙니다: " +
                $"{joint.jointType}"
            );
        }

        joints[jointName] = joint;

        ArticulationDrive drive = joint.xDrive;
        drive.stiffness = 10000f;
        drive.damping = 1000f;
        drive.forceLimit = 10000f;
        joint.xDrive = drive;

        Debug.Log(
            $"[ZKeep] 관절 등록: {jointName} → {joint.name}, " +
            $"범위={drive.lowerLimit}~{drive.upperLimit}도"
        );
    }

    void OnJointState(JointStateMsg msg)
    {
        Debug.Log(
            $"[ZKeep] JointState 수신: " +
            $"name=[{string.Join(", ", msg.name)}], " +
            $"position=[{string.Join(", ", msg.position)}]"
        );

        int count = Math.Min(msg.name.Length, msg.position.Length);

        for (int i = 0; i < count; i++)
        {
            string receivedName = NormalizeJointName(msg.name[i]);

            if (!joints.TryGetValue(
                    receivedName,
                    out ArticulationBody joint))
            {
                Debug.LogWarning(
                    $"[ZKeep] 등록되지 않은 관절 이름: {msg.name[i]}"
                );
                continue;
            }

            float targetDegrees =
                (float)(msg.position[i] * Mathf.Rad2Deg);

            ArticulationDrive drive = joint.xDrive;

            float clampedTarget = Mathf.Clamp(
                targetDegrees,
                drive.lowerLimit,
                drive.upperLimit
            );

            drive.target = clampedTarget;
            joint.xDrive = drive;

            Debug.Log(
                $"[ZKeep] {receivedName}: " +
                $"수신={targetDegrees:F1}도, 적용={clampedTarget:F1}도"
            );
        }
    }

    string NormalizeJointName(string jointName)
    {
        if (string.IsNullOrEmpty(jointName))
            return jointName;

        int slashIndex = jointName.LastIndexOf('/');

        if (slashIndex >= 0 && slashIndex < jointName.Length - 1)
            return jointName.Substring(slashIndex + 1);

        return jointName;
    }
}