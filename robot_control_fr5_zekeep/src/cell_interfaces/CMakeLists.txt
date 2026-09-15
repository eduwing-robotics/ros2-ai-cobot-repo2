cmake_minimum_required(VERSION 3.8)
project(cell_interfaces)

find_package(ament_cmake REQUIRED)
find_package(rosidl_default_generators REQUIRED)
find_package(action_msgs REQUIRED)
find_package(geometry_msgs REQUIRED)
find_package(builtin_interfaces REQUIRED)

rosidl_generate_interfaces(${PROJECT_NAME}
  "action/ExecuteTask.action"
  "srv/CellControl.srv"
  "msg/PickCandidate.msg"
  "srv/GetPickPose.srv"
  DEPENDENCIES action_msgs geometry_msgs builtin_interfaces
)

ament_package()
