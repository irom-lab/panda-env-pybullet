from abc import ABC
import numpy as np
import pybullet_data

from panda_gym.base_env import BaseEnv
from alano.geometry.transform import quatMult, euler2quat, quat2rot, log_rot
from alano.geometry.scaling import traj_time_scaling
from alano.bullet.kinematics import full_jacob_pb


class GraspEnv(BaseEnv, ABC):
    def __init__(
        self,
        task=None,
        renders=False,
        img_h=128,
        img_w=128,
        use_rgb=False,
        use_depth=True,
        max_steps_train=100,
        max_steps_eval=100,
        done_type='fail',
        #
        mu=0.5,
        sigma=0.01,
        camera_params=None,
    ):
        """
        Args:
            task (str, optional): the name of the task. Defaults to None.
            img_H (int, optional): the height of the image. Defaults to 128.
            img_W (int, optional): the width of the image. Defaults to 128.
            use_rgb (bool, optional): whether to use RGB image. Defaults to
                True.
            render (bool, optional): whether to render the environment.
                Defaults to False.
            max_steps_train (int, optional): the maximum number of steps to
                train. Defaults to 100.
            max_steps_eval (int, optional): the maximum number of steps to
                evaluate. Defaults to 100.
            done_type (str, optional): the type of the done. Defaults to
                'fail'.
        """
        super(GraspEnv, self).__init__(
            task=task,
            renders=renders,
            img_h=img_h,
            img_w=img_w,
            use_rgb=use_rgb,
            use_depth=use_depth,
            max_steps_train=max_steps_train,
            max_steps_eval=max_steps_eval,
            done_type=done_type,
        )
        self._mu = mu
        self._sigma = sigma

        # Object id
        self._obj_id_list = []
        self._obj_initial_height_list = {}
        self._urdf_root = pybullet_data.getDataPath()

        # Camera info
        self._camera_params = camera_params

    @property
    def action_dim(self):
        """
        Dimension of robot action - x,y,yaw
        """
        return 3

    @property
    def init_joint_angles(self):
        """
        Initial joint angles for the task - [0.5, 0, 0.3], straight down - ee to finger tip is 15.5cm
        """
        return [
            0, -0.020, 0, -2.347, 0, 2.327, 0.779, 0, -np.pi / 4,
            self._finger_open_pos, 0.00, self._finger_open_pos, 0.00
        ]

    def report(self):
        """
        Print information of robot dynamics and observation.
        """
        raise NotImplementedError

    def visualize(self):
        """
        Visualize trajectories and value functions.
        """
        raise NotImplementedError

    def reset_task(self, task):
        """
        Reset the task for the environment. Load object - task
        """
        # Clean table
        for obj_id in self._obj_id_list:
            self._p.removeBody(obj_id)

        # Reset obj info
        self._obj_id_list = []
        self._obj_initial_height_list = {}

        # Add bin
        obj_collision_id = self._p.createCollisionShape(shapeType=self._p.GEOM_MESH,
            fileName='data/private/bin/bin.obj',
            flags=self._p.GEOM_FORCE_CONCAVE_TRIMESH)
        obj_visual_id = self._p.createVisualShape(shapeType=self._p.GEOM_MESH,
            fileName='data/private/bin/bin.obj', rgbaColor=[0.95,0.95,0.95,1])
        obj_id = self._p.createMultiBody(baseMass=0, baseCollisionShapeIndex=obj_collision_id, baseVisualShapeIndex=obj_visual_id,
            basePosition=[0.5,0,0],
            baseOrientation=self._p.getQuaternionFromEuler(
                    [0, 0, np.pi/2]))
        self._obj_id_list += [obj_id]

        # Load all
        obj_path_list = task['obj_path_list']
        obj_init_state_all = task['obj_init_state_all']
        for obj_path, obj_init_state in zip(obj_path_list, obj_init_state_all):
            obj_init_state[-2] += 0.1
            obj_id = self._p.loadURDF(
                obj_path,
                basePosition=obj_init_state[:-1],
                baseOrientation=self._p.getQuaternionFromEuler(
                    [0, 0, obj_init_state[-1]]))
            self._obj_id_list += [obj_id]

            # Infer number of links - change dynamics for each
            num_joint = self._p.getNumJoints(obj_id)
            link_all = [-1] + [*range(num_joint)]
            for link_id in link_all:
                self._p.changeDynamics(
                    obj_id,
                    link_id,
                    lateralFriction=self._mu,
                    spinningFriction=self._sigma,
                    frictionAnchor=1,
                )

        # Let objects settle (actually do not need since we know the height of object and can make sure it spawns very close to table level)
        for _ in range(10):
            self._p.stepSimulation()

        # Record object initial height (for comparing with final height when checking if lifted). Note that obj_initial_height_list is a dict
        for obj_id in self._obj_id_list:
            pos, _ = self._p.getBasePositionAndOrientation(obj_id)
            self._obj_initial_height_list[obj_id] = pos[2]

    def reset(self, task=None):
        """
        Reset the environment, including robot state, task, and obstacles.
        Initialize pybullet client if 1st time.
        """
        if self._physics_client_id < 0:

            # Initialize PyBullet instance
            self.init_pb()

            # Load table
            self._plane_id = self._p.loadURDF(self._urdf_root + '/plane.urdf',
                                              basePosition=[0, 0, -1],
                                              useFixedBase=1)
            self._table_id = self._p.loadURDF(
                self._urdf_root + '/table/table.urdf',
                basePosition=[0.400, 0.000, -0.630 + 0.005],
                baseOrientation=[0., 0., 0., 1.0],
                useFixedBase=1)

            # Set friction coefficient for table
            self._p.changeDynamics(
                self._table_id,
                -1,
                lateralFriction=self._mu,
                spinningFriction=self._sigma,
                frictionAnchor=1,
            )

        # Load arm, no need to settle (joint angle set instantly)
        self.reset_robot(self._mu, self._sigma)

        # Reset task
        self.reset_task(task)

        # Reset timer
        self.step_elapsed = 0

        return self._get_obs()

    def step(self, action):
        """
        Gym style step function. Apply action, move robot, get observation,
        calculate reward, check if done.
        
        Assume action in [x,y,yaw]
        """

        # Set arm to starting pose
        initial_ee_pos_before_img = np.array([0.3, -0.5, 0.25])
        initial_ee_orn = np.array([1.0, 0.0, 0.0, 0.0])  # straight down
        self.reset_arm_joints_ik(initial_ee_pos_before_img, initial_ee_orn)
        self.grasp(targetVel=0.10)  # open gripper

        # Execute, reset ik on top of object, reach down, grasp, lift, check success
        ee_pos = action
        ee_pos_before = action + np.array([0, 0, 0.10])
        ee_pos_after = action + np.array([0, 0, 0.05])
        ee_orn = quatMult(euler2quat([action[-1], 0., 0.]), initial_ee_orn)
        for _ in range(3):
            self.reset_arm_joints_ik(ee_pos_before, ee_orn)
            self._p.stepSimulation()
        self.move(ee_pos, absolute_global_quat=ee_orn, numSteps=300)
        self.grasp(targetVel=-0.10)  # always close gripper
        self.move(ee_pos, absolute_global_quat=ee_orn,
                  numSteps=100)  # keep pose until gripper closes
        self.move(ee_pos_after, absolute_global_quat=ee_orn,
                  numSteps=150)  # lift

        # Check if all objects removed
        self.clear_obj()
        if len(self._obj_id_list) == 0:
            reward = 1
        else:
            reward = 0
        return self._get_obs(self.camera_params), reward, True, {}

    def clear_obj(self):
        height = []
        obj_to_be_removed = []
        for obj_id in self._obj_id_list:
            pos, _ = self._p.getBasePositionAndOrientation(obj_id)
            height += [pos[2]]
            if pos[2] - self._obj_initial_height_list[obj_id] > 0.03:
                obj_to_be_removed += [obj_id]

        for obj_id in obj_to_be_removed:
            self._p.removeBody(obj_id)
            self._obj_id_list.remove(obj_id)

    def move(
        self,
        absolute_pos=None,
        relative_pos=None,
        absolute_global_euler=None,  # preferred
        relative_global_euler=None,  # preferred
        relative_local_euler=None,  # not using
        absolute_global_quat=None,  # preferred
        relative_azi=None,  # for arm
        num_steps=50,
        max_joint_vel=0.20,
        # time_step=0,
        # check_contact=False,
        # obj_id=None,
        pos_gain=20,
        vel_gain=5,
        # relative_quat=None,  # never use relative quat
    ):

        # Get trajectory
        ee_pos, ee_quat = self._get_ee()

        # Determine target pos
        if absolute_pos is not None:
            target_pos = absolute_pos
        elif relative_pos is not None:
            target_pos = ee_pos + relative_pos
        else:
            target_pos = ee_pos

        # Determine target orn
        if absolute_global_euler is not None:
            target_orn = euler2quat(absolute_global_euler)
        elif relative_global_euler is not None:
            target_orn = quatMult(euler2quat(relative_global_euler), ee_quat)
        elif relative_local_euler is not None:
            target_orn = quatMult(ee_quat, euler2quat(relative_local_euler))
        elif absolute_global_quat is not None:
            target_orn = absolute_global_quat
        elif relative_azi is not None:
            # Extrinsic yaw
            target_orn = quatMult(euler2quat([relative_azi[0], 0, 0]), ee_quat)
            # Intrinsic pitch
            target_orn = quatMult(target_orn,
                                  euler2quat([0, relative_azi[1], 0]))
        # elif relative_quat is not None:
        # 	target_orn = quatMult(ee_quat, relative_quat)
        else:
            target_orn = np.array([1.0, 0., 0., 0.])

        # Get trajectory
        traj_pos = traj_time_scaling(start_pos=ee_pos,
                                     end_pos=target_pos,
                                     num_steps=num_steps)

        # Run steps
        num_steps = len(traj_pos)
        for step in range(num_steps):

            # Get joint velocities from error tracking control, takes 0.2ms
            joint_dot = self.traj_tracking_vel(target_pos=traj_pos[step],
                                               target_quat=target_orn,
                                               pos_gain=pos_gain,
                                               vel_gain=vel_gain)

            # Send velocity commands to joints
            for i in range(self._num_joint_arm):
                self._p.setJointMotorControl2(self._panda_id,
                                              i,
                                              self._p.VELOCITY_CONTROL,
                                              targetVelocity=joint_dot[i],
                                              force=self._max_joint_force[i],
                                              maxVelocity=max_joint_vel)

            # Keep gripper current velocity
            self._p.setJointMotorControl2(self._panda_id,
                                          self._left_finger_joint_id,
                                          self._p.VELOCITY_CONTROL,
                                          targetVelocity=self._finger_cur_vel,
                                          force=self._max_finger_force,
                                          maxVelocity=0.10)
            self._p.setJointMotorControl2(self._panda_id,
                                          self._right_finger_joint_id,
                                          self._p.VELOCITY_CONTROL,
                                          targetVelocity=self._finger_cur_vel,
                                          force=self._max_finger_force,
                                          maxVelocity=0.10)

            # # Quit if contact at either finger
            # if check_contact:
            #     contact = self.check_contact(objId, both=False)
            #     if contact:
            #         return timeStep, False

            # Step simulation, takes 1.5ms
            self._p.stepSimulation()
            # timeStep += 1
            # return timeStep, True

    def traj_tracking_vel(self,
                          target_pos,
                          target_quat,
                          pos_gain=20,
                          vel_gain=5):  #Change gains based off mouse
        ee_pos, ee_quat = self._get_ee()

        ee_pos_error = target_pos - ee_pos
        # ee_orn_error = log_rot(quat2rot(target_quat)@(quat2rot(ee_quat).T))  # in spatial frame
        ee_orn_error = log_rot(
            quat2rot(target_quat).dot(
                (quat2rot(ee_quat).T)))  # in spatial frame

        joint_poses = list(
            np.hstack((self._get_arm_joints(), np.array([0, 0,
                                                         0]))))  # add fingers
        ee_state = self._p.getLinkState(self._panda_id,
                                        self._ee_link_id,
                                        computeLinkVelocity=1,
                                        computeForwardKinematics=1)
        # Get the Jacobians for the CoM of the end-effector link. Note that in this example com_rot = identity, and we would need to use com_rot.T * com_trn. The localPosition is always defined in terms of the link frame coordinates.
        zero_vec = list(np.zeros_like(joint_poses))
        jac_t, jac_r = self._p.calculateJacobian(
            self._panda_id, self._ee_link_id, ee_state[2], joint_poses,
            zero_vec, zero_vec)  # use localInertialFrameOrientation
        jac_sp = full_jacob_pb(
            jac_t, jac_r)[:, :7]  # 6x10 -> 6x7, ignore last three columns
        try:
            joint_dot = np.linalg.pinv(jac_sp).dot((np.hstack(
                (pos_gain * ee_pos_error,
                 vel_gain * ee_orn_error)).reshape(6, 1)))  # pseudo-inverse
        except np.linalg.LinAlgError:
            joint_dot = np.zeros((7, 1))
        return joint_dot