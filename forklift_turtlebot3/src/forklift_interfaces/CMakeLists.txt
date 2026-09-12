cmake_minimum_required(VERSION 3.8)
project(forklift_interfaces)

find_package(ament_cmake REQUIRED)
find_package(geometry_msgs REQUIRED)
find_package(rosidl_default_generators REQUIRED)

rosidl_generate_interfaces(${PROJECT_NAME}
  "msg/ForkliftControlCommand.msg"
  "msg/ForkliftTaskStatus.msg"
  "msg/LiftCommand.msg"
  "msg/LiftState.msg"
  "msg/DockCommand.msg"
  "msg/DockState.msg"
  "msg/MarkerDetection.msg"
  "action/ExecuteTransport.action"
  "action/ReturnHome.action"
  DEPENDENCIES geometry_msgs
)

ament_export_dependencies(rosidl_default_runtime)
ament_package()
