from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.conditions import IfCondition, UnlessCondition
from launch.substitutions import (
    LaunchConfiguration, PathJoinSubstitution, PythonExpression,
)
from launch_ros.actions import Node
from launch_ros.parameter_descriptions import ParameterValue
from launch_ros.substitutions import FindPackageShare


def generate_launch_description():
    config_file = LaunchConfiguration('config_file')
    calibration_file = LaunchConfiguration('calibration_file')
    use_mock_gpio = LaunchConfiguration('use_mock_gpio')
    use_mock_lift = LaunchConfiguration('use_mock_lift')
    use_mock_docking = LaunchConfiguration('use_mock_docking')
    use_mock_route = LaunchConfiguration('use_mock_route')
    enable_action_server = LaunchConfiguration('enable_action_server')
    enable_odom_axis_driver = LaunchConfiguration('enable_odom_axis_driver')
    enable_aruco_detector = LaunchConfiguration('enable_aruco_detector')
    enable_lift_servo = LaunchConfiguration('enable_lift_servo')
    initial_location_code = LaunchConfiguration('initial_location_code')

    default_config = PathJoinSubstitution(
        [FindPackageShare('forklift_control'), 'config', 'forklift_params.yaml']
    )
    default_calibration = PathJoinSubstitution(
        [FindPackageShare('forklift_control'), 'config', 'calibration.yaml']
    )

    return LaunchDescription([
        DeclareLaunchArgument('config_file', default_value=default_config),
        DeclareLaunchArgument('calibration_file', default_value=default_calibration),
        DeclareLaunchArgument('use_mock_gpio', default_value='false'),
        DeclareLaunchArgument('use_mock_lift', default_value='false'),
        DeclareLaunchArgument('use_mock_docking', default_value='false'),
        DeclareLaunchArgument('use_mock_route', default_value='false'),
        DeclareLaunchArgument('enable_action_server', default_value='true'),
        DeclareLaunchArgument('enable_odom_axis_driver', default_value='true'),
        DeclareLaunchArgument('enable_aruco_detector', default_value='true'),
        # 실제 UDP 리프트 데몬이 localhost:5005에서 동작하는 Pi에서만 true.
        DeclareLaunchArgument('enable_lift_servo', default_value='false'),
        DeclareLaunchArgument('initial_location_code', default_value='HOME'),

        Node(
            package='forklift_control',
            executable='transport_action_server',
            name='transport_action_server',
            output='screen',
            parameters=[
                config_file,
                calibration_file,
                {
                    'use_mock_route': ParameterValue(
                        use_mock_route, value_type=bool
                    ),
                    'use_mock_lift': ParameterValue(
                        use_mock_lift, value_type=bool
                    ),
                    'initial_location_code': ParameterValue(
                        initial_location_code, value_type=str
                    ),
                },
            ],
            condition=IfCondition(enable_action_server),
        ),
        Node(
            package='forklift_control',
            executable='lift_servo',
            name='forklift_lift_servo',
            output='screen',
            parameters=[
                config_file,
                calibration_file,
                {
                    'use_mock_lift': ParameterValue(
                        use_mock_lift, value_type=bool
                    ),
                    'use_mock_gpio': ParameterValue(
                        use_mock_gpio, value_type=bool
                    ),
                },
            ],
            condition=IfCondition(PythonExpression([
                "'", enable_lift_servo, "'.lower() == 'true' and '",
                use_mock_lift, "'.lower() == 'false'",
            ])),
        ),
        Node(
            package='forklift_control',
            executable='odom_axis_driver',
            name='odom_axis_driver',
            output='screen',
            parameters=[config_file, calibration_file],
            remappings=[('/cmd_vel', '/forklift/cmd_vel/route')],
            condition=IfCondition(enable_odom_axis_driver),
        ),
        Node(
            package='forklift_control',
            executable='aruco_detector',
            name='aruco_detector',
            output='screen',
            parameters=[config_file, calibration_file],
            respawn=True,
            respawn_delay=2.0,
            condition=IfCondition(PythonExpression([
                "'", enable_aruco_detector, "'.lower() == 'true' and '",
                use_mock_docking, "'.lower() == 'false'",
            ])),
        ),
        Node(
            package='forklift_control',
            executable='aruco_docking',
            name='aruco_docking',
            output='screen',
            parameters=[config_file, calibration_file],
            remappings=[('/cmd_vel', '/forklift/cmd_vel/dock')],
            condition=UnlessCondition(use_mock_docking),
        ),
        Node(
            package='forklift_control',
            executable='mock_docking',
            name='mock_aruco_docking',
            output='screen',
            parameters=[config_file, calibration_file],
            condition=IfCondition(use_mock_docking),
        ),
    ])
