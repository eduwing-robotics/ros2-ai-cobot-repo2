"""Pi-side velocity arbiter and optional Unity WebSocket gateway."""

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.conditions import IfCondition
from launch.substitutions import LaunchConfiguration, PathJoinSubstitution
from launch_ros.actions import Node
from launch_ros.parameter_descriptions import ParameterValue
from launch_ros.substitutions import FindPackageShare


def generate_launch_description():
    config_file = LaunchConfiguration('config_file')
    enable_manual_websocket = LaunchConfiguration('enable_manual_websocket')
    manual_ws_bind_host = LaunchConfiguration('manual_ws_bind_host')
    manual_ws_port = LaunchConfiguration('manual_ws_port')
    manual_ws_token = LaunchConfiguration('manual_ws_token')

    default_config = PathJoinSubstitution(
        [FindPackageShare('forklift_control'), 'config', 'forklift_params.yaml']
    )

    return LaunchDescription([
        DeclareLaunchArgument('config_file', default_value=default_config),
        DeclareLaunchArgument('enable_manual_websocket', default_value='true'),
        DeclareLaunchArgument('manual_ws_bind_host', default_value='0.0.0.0'),
        DeclareLaunchArgument('manual_ws_port', default_value='8765'),
        DeclareLaunchArgument('manual_ws_token', default_value=''),
        Node(
            package='forklift_control',
            executable='cmd_vel_arbiter',
            name='cmd_vel_arbiter',
            output='screen',
            parameters=[config_file],
        ),
        Node(
            package='forklift_control',
            executable='manual_websocket_gateway',
            name='manual_websocket_gateway',
            output='screen',
            parameters=[
                config_file,
                {
                    'manual_ws_bind_host': ParameterValue(
                        manual_ws_bind_host, value_type=str
                    ),
                    'manual_ws_port': ParameterValue(
                        manual_ws_port, value_type=int
                    ),
                    'manual_ws_token': ParameterValue(
                        manual_ws_token, value_type=str
                    ),
                },
            ],
            condition=IfCondition(enable_manual_websocket),
        ),
    ])
