# Main Server transport integration

Unity does not connect to ROS 2 directly. Main Server/FMS sends logical location
codes to the TurtleBot ActionServer and converts feedback/results for Unity.

## Action endpoints

| Action type | Endpoint | Purpose |
|---|---|---|
| `forklift_interfaces/action/ExecuteTransport` | `/forklift/execute_transport` | One pickup-to-dropoff transport |
| `forklift_interfaces/action/ReturnHome` | `/forklift/return_home` | Return to HOME and park |

The robot identifier used by Main Server is `forklift_01`. It is routing
metadata outside the Action goal because this robot has its own Action endpoint.

## ExecuteTransport goal

```text
string req_id
int64 job_id
int64 delivery_id
string pickup_code
string dropoff_code
```

Supported logical codes:

| Code | Physical location | ArUco marker |
|---|---|---|
| `RACK1` | Point A rack | 41 |
| `RACK2` | Point B rack | 40 |
| `DROP` | Delivery/drop zone | 42 |

Main Server never sends x/y/yaw or marker IDs. The TurtleBot owns the route,
marker mapping, HOME detour, docking, and backoff policy.

## Live mobile robot pose

Main Server subscribes to the following ROS 2 topic and forwards it to Unity as
`mobile_robot_pose`:

```text
topic: /forklift/mobile_robot_pose
type: geometry_msgs/msg/PoseStamped
rate: 10 Hz
robot_id: forklift_01 (added by Main Server)
```

`header.frame_id` is `map` for the Unity contract. Its origin is the fixed
physical HOME pose; it is not a Nav2/SLAM map. Position is in meters and
orientation is a quaternion. The pose is logical odometry after HOME and
ArUco landmark re-anchoring, rather than raw `/odom`.

Example:

```bash
ros2 action send_goal /forklift/execute_transport \
  forklift_interfaces/action/ExecuteTransport \
  "{req_id: 'req-001', job_id: 10, delivery_id: 20, pickup_code: 'RACK1', dropoff_code: 'DROP'}" \
  --feedback
```

## ReturnHome goal

```text
string req_id
```

Example:

```bash
ros2 action send_goal /forklift/return_home \
  forklift_interfaces/action/ReturnHome \
  "{req_id: 'return-001'}" \
  --feedback
```

## Physical sequence

A successful ExecuteTransport goal performs:

1. Move from the current logical location to the pickup location.
2. ArUco precision dock.
3. Raise the pallet from HEIGHT_2 to the carrying HEIGHT_3 preset.
4. Back off 0.20 m.
5. Return through HOME/HOME_BEHIND and align to HOME marker 41.
6. Move to the dropoff location.
7. ArUco precision dock.
8. Lower the pallet to HEIGHT_2 at either a rack or DROP.
9. Back off 0.20 m without an additional relative lift movement.
10. Return the Action result.

`ReturnHome` always performs the final ArUco alignment at HOME, parks the lift at
HEIGHT_2, and then backs off 0.20 m. Its final logical position is
`HOME_BEHIND`. The robot does not automatically start another job afterward.

All RACK-to-DROP and DROP-to-RACK paths pass through the validated HOME corridor.
DROP approach uses `GO_HOME_BEHIND` followed by marker 42 search docking.
RACK2 approach uses `GO_RACK2_LANE`. RACK1 is visible directly from HOME.

A completed action leaves the robot at the dropoff staging position. This lets
the next action pick up the empty pallet from the same location without an
unnecessary HOME round trip.

The production flow is sent as separate, observable actions:

```text
RACK1 -> DROP
DROP  -> RACK1
RACK2 -> DROP
DROP  -> RACK2
ReturnHome
```

## Feedback and result mapping

ExecuteTransport feedback contains `phase`, `progress`, and `detail`.
Main Server keeps the goal's `req_id`, `job_id`, and `delivery_id` and adds
those values to Unity messages.

Terminal result fields:

- `status`: `SUCCEEDED`, `FAILED`, or `CANCELED`
- `error_code`: empty on success, otherwise a stable failure category
- `detail`: the failed phase and the route/docking device message

Only the terminal Action result is the completion decision. Feedback progress is
for UI display.

## ROS 2 network

The Main Server, TurtleBot PC, and TurtleBot Pi all use:

~~~bash
export ROS_DOMAIN_ID=73
export RMW_IMPLEMENTATION=rmw_cyclonedds_cpp
export CYCLONEDDS_URI=file://$HOME/cyclonedds_unicast.xml
~~~

Multicast is disabled. The TurtleBot PC XML binds to
`wlxb0386cf6fa74` (`192.168.20.40`) and peers with the Pi
(`192.168.20.100`) and Main Server (`192.168.20.20`). The deployable XML
templates are `src/forklift_control/config/cyclonedds_pc_unicast.xml` and
`src/forklift_control/config/cyclonedds_pi_unicast.xml`.

## PC startup

Before starting the PC controllers, keep the Pi-side velocity arbiter running.
It is the only node allowed to publish the TurtleBot's physical `/cmd_vel`; the
optional WebSocket gateway is hosted beside it on the Pi.

```bash
source /opt/ros/jazzy/setup.bash
source ~/forklift_ws/install/setup.bash
export ROS_DOMAIN_ID=73
export RMW_IMPLEMENTATION=rmw_cyclonedds_cpp
export CYCLONEDDS_URI=file:///home/pi/cyclonedds_unicast.xml

ros2 launch forklift_control forklift_pi.launch.py
```

After the Pi publishes `/odom`, `/scan`, and `/camera/image_raw`, run on
the PC:

```bash
cd ~/ros2-ai-cobot-repo2/forklift_turtlebot3
source /opt/ros/jazzy/setup.bash
source install/setup.bash
export ROS_DOMAIN_ID=73
export RMW_IMPLEMENTATION=rmw_cyclonedds_cpp
unset ROS_STATIC_PEERS
DDS_PC_PATH="$(ros2 pkg prefix --share forklift_control)/config/cyclonedds_pc_unicast.xml"
export CYCLONEDDS_URI="file://$DDS_PC_PATH"

ros2 launch forklift_control forklift.launch.py \
  enable_action_server:=true \
  use_mock_lift:=false
```

This launch starts the odom route driver, ArUco detector/docking controller, and
the transport ActionServer. Do not run duplicate copies of those nodes.

The lift adapter runs on the Pi next to the UDP daemon. Before accepting a real
transport goal, visually confirm that the lift is at physical HEIGHT_2 and send
one ZERO command. The adapter reads the stored J2 step value and uses the daemon's
Y command to restore that coordinate without changing the existing J2/J3 presets
or limits. It then reports AT_HEIGHT_2. Never send ZERO when the physical height
is unknown.
