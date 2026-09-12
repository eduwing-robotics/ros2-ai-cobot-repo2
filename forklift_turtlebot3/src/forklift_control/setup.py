from setuptools import find_packages, setup

package_name = 'forklift_control'

setup(
    name=package_name,
    version='0.2.0',
    packages=find_packages(),
    data_files=[
        ('share/ament_index/resource_index/packages', ['resource/' + package_name]),
        ('share/' + package_name, ['package.xml']),
        ('share/' + package_name + '/launch', [
            'launch/forklift.launch.py',
            'launch/forklift_pi.launch.py',
        ]),
        ('share/' + package_name + '/config', [
            'config/forklift_params.yaml',
            'config/calibration.yaml',
            'config/cyclonedds_pc_unicast.xml',
            'config/cyclonedds_pi_unicast.xml',
        ]),
    ],
    install_requires=['setuptools'],
    zip_safe=True,
    maintainer='forklift_turtlebot3 team',
    maintainer_email='eduwing-robotics@users.noreply.github.com',
    description='ROS2 TurtleBot forklift controller',
    license='Apache-2.0',
    entry_points={
        'console_scripts': [
            'lift_servo = forklift_control.lift_udp:main',
            'mock_docking = forklift_control.mock_docking:main',
            'aruco_docking = forklift_control.aruco_docking:main',
            'aruco_detector = forklift_control.aruco_detector:main',
            'dock_reference_recorder = forklift_control.dock_reference_recorder:main',
            'odom_axis_driver = forklift_control.odom_axis_driver:main',
            'transport_action_server = forklift_control.transport_action_server:main',
            'calibration_validator = forklift_control.calibration_validator:main',
            'lift_commissioning = forklift_control.lift_commissioning:main',
            'cmd_vel_arbiter = forklift_control.cmd_vel_arbiter:main',
            'manual_websocket_gateway = forklift_control.manual_websocket_gateway:main',
        ],
    },
)
