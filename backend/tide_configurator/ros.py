"""Host ROS 2 subscriber for the browser VIO view."""
import asyncio
import os
import threading


class RosTelemetry:
    def __init__(self, workspace, on_pose, on_tracking):
        self.workspace = workspace
        self.on_pose = on_pose
        self.on_tracking = on_tracking
        self.thread = None
        self.stop_event = threading.Event()
        self.ready = threading.Event()
        self.error = ''

    @property
    def running(self):
        return self.thread is not None and self.thread.is_alive()

    async def start(self):
        if self.running:
            return
        if self.thread is not None:
            await self.stop()
        if os.environ.get('ROS_VERSION') != '2':
            raise RuntimeError('未加载电脑端 ROS 2 环境；请通过 run.sh 启动 TideConfigurator')
        self.error = ''
        self.stop_event.clear()
        self.ready.clear()
        loop = asyncio.get_running_loop()
        self.thread = threading.Thread(target=self._subscribe, args=(loop,), daemon=True,
                                       name='tide-ros-subscriber')
        self.thread.start()
        if not await asyncio.to_thread(self.ready.wait, 5) or self.error:
            reason = self.error or 'ROS 2 订阅节点启动超时'
            await self.stop()
            raise RuntimeError(reason)

    def _subscribe(self, loop):
        context = None
        node = None
        executor = None
        try:
            import rclpy
            from rclpy.context import Context
            from rclpy.node import Node
            from rclpy.executors import SingleThreadedExecutor
            from rclpy.qos import qos_profile_sensor_data
            from geometry_msgs.msg import PoseStamped
            from std_msgs.msg import Bool

            context = Context()
            rclpy.init(context=context)
            node = Node('tide_configurator_vio', context=context)
            node.create_subscription(PoseStamped, '/tidesense/pose',
                                     lambda msg: loop.call_soon_threadsafe(self.on_pose, msg),
                                     qos_profile_sensor_data)
            node.create_subscription(Bool, '/tidesense/tracking',
                                     lambda msg: loop.call_soon_threadsafe(self.on_tracking, bool(msg.data)),
                                     qos_profile_sensor_data)
            executor = SingleThreadedExecutor(context=context)
            executor.add_node(node)
            self.ready.set()
            while not self.stop_event.is_set() and context.ok():
                executor.spin_once(timeout_sec=.2)
        except Exception as exc:
            self.error = f'ROS 2 订阅失败：{exc}'
            self.ready.set()
        finally:
            if executor is not None:
                executor.shutdown()
            if node is not None:
                node.destroy_node()
            if context is not None and context.ok():
                context.shutdown()

    async def stop(self):
        self.stop_event.set()
        if self.thread is not None:
            await asyncio.to_thread(self.thread.join, 2)
            self.thread = None
