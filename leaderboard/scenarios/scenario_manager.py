#!/usr/bin/env python

# Copyright (c) 2018-2019 Intel Corporation
#
# This work is licensed under the terms of the MIT license.
# For a copy, see <https://opensource.org/licenses/MIT>.

"""
This module provides the Scenario and ScenarioManager implementations.
These must not be modified and are for reference only!
"""

from __future__ import print_function
import signal
import sys
import time
import threading

import py_trees
import carla

from srunner.scenariomanager.carla_data_provider import CarlaDataProvider
from srunner.scenariomanager.timer import GameTime, TimeOut
from srunner.scenariomanager.watchdog import Watchdog

from leaderboard.autoagents.agent_wrapper import AgentWrapper


class ScenarioManager(object):

    """
    Basic scenario manager class. This class holds all functionality
    required to start, run and stop a scenario.

    The user must not modify this class.

    To use the ScenarioManager:
    1. Create an object via manager = ScenarioManager()
    2. Load a scenario via manager.load_scenario()
    3. Trigger the execution of the scenario manager.run_scenario()
       This function is designed to explicitly control start and end of
       the scenario execution
    4. If needed, cleanup with manager.stop_scenario()
    """

    def __init__(self, debug_mode=False, challenge_mode=False, track=None, timeout=10.0):
        """
        Setups up the parameters, which will be filled at load_scenario()
        """
        self.scenario = None
        self.scenario_tree = None
        self.scenario_class = None
        self.ego_vehicles = None
        self.other_actors = None

        self._debug_mode = debug_mode
        self._challenge_mode = challenge_mode
        self._track = track
        self._agent = None
        self._running = False
        self._timestamp_last_run = 0.0
        self._timeout = timeout
        self._watchdog = Watchdog(float(self._timeout))

        self.scenario_duration_system = 0.0
        self.scenario_duration_game = 0.0
        self.start_system_time = None
        self.end_system_time = None

        # Register the scenario tick as callback for the CARLA world
        # Use the callback_id inside the signal handler to allow external interrupts
        signal.signal(signal.SIGINT, self._signal_handler)

    def _signal_handler(self, signum, frame):
        """
        Terminate scenario ticking when receiving a signal interrupt
        """
        self._running = False
        if not self.get_running_status():
            raise RuntimeError("Timeout occured during scenario execution")

    def _reset(self):
        """
        Reset all parameters
        """
        self._running = False
        self._timestamp_last_run = 0.0
        self.scenario_duration_system = 0.0
        self.scenario_duration_game = 0.0
        self.start_system_time = None
        self.end_system_time = None
        GameTime.restart()

    def load_scenario(self, scenario, agent=None):
        """
        Load a new scenario
        """
        self._reset()
        self._agent = AgentWrapper(agent, self._challenge_mode) if agent else None
        self.scenario_class = scenario
        self.scenario = scenario.scenario
        self.scenario_tree = self.scenario.scenario_tree
        self.ego_vehicles = scenario.ego_vehicles
        self.other_actors = scenario.other_actors

        CarlaDataProvider.register_actors(self.ego_vehicles)
        CarlaDataProvider.register_actors(self.other_actors)
        # To print the scenario tree uncomment the next line
        # py_trees.display.render_dot_tree(self.scenario_tree)

        if self._agent is not None:
            self._agent.setup_sensors(self.ego_vehicles[0], self._debug_mode, self._track)

    def run_scenario(self):
        """
        Trigger the start of the scenario and wait for it to finish/fail
        """
        print("ScenarioManager: Running scenario {}".format(self.scenario_tree.name))
        self.start_system_time = time.time()
        start_game_time = GameTime.get_time()

        self._watchdog.start()
        self._running = True

        while self._running:
            timestamp = None
            world = CarlaDataProvider.get_world()
            if world:
                snapshot = world.get_snapshot()
                if snapshot:
                    timestamp = snapshot.timestamp
            if timestamp:
                self._tick_scenario(timestamp)

        self._watchdog.stop()

        self.end_system_time = time.time()
        end_game_time = GameTime.get_time()

        self.scenario_duration_system = self.end_system_time - \
            self.start_system_time
        self.scenario_duration_game = end_game_time - start_game_time

        self._console_message()

    def _console_message(self):
        """
        Message that will be displayed via console
        """
        def get_symbol(value, desired_value, high=True):
            """
            Returns a tick or a cross depending on the values
            """
            tick = '\033[92m'+'O'+'\033[0m'
            cross = '\033[91m'+'X'+'\033[0m'

            multiplier = 1 if high else -1

            if multiplier*value >= desired_value:
                symbol = tick
            else:
                symbol = cross

            return symbol

        if self.scenario_tree.status == py_trees.common.Status.RUNNING:
            # If still running, all the following is None, so no point continuing
            print("\n> Something happened during the simulation. Was it manually shutdown?\n")
            return

        blackv = py_trees.blackboard.Blackboard()
        route_completed = blackv.get("RouteCompletion")
        collisions = blackv.get("Collision")
        outside_route_lanes = blackv.get("OutsideRouteLanes")
        stop_signs = blackv.get("RunningStop")
        red_light = blackv.get("RunningRedLight")
        in_route = blackv.get("InRoute")

        # If something failed, stop
        if [x for x in (collisions, outside_route_lanes, stop_signs, red_light, in_route) if x is None]:
            return

        if blackv.get("RouteCompletion") >= 99:
            route_completed = 100
        else:
            route_completed = blackv.get("RouteCompletion")
        outside_route_lanes = float(outside_route_lanes)

        route_symbol = get_symbol(route_completed, 100, True)
        collision_symbol = get_symbol(collisions, 0, False)
        outside_symbol = get_symbol(outside_route_lanes, 0, False)
        red_light_symbol = get_symbol(red_light, 0, False)
        stop_symbol = get_symbol(stop_signs, 0, False)

        if self.scenario_tree.status == py_trees.common.Status.FAILURE:
            if not in_route:
                message = "> FAILED: The actor deviated from the route"
            else:
                message = "> FAILED: The actor didn't finish the route"
        elif self.scenario_tree.status == py_trees.common.Status.SUCCESS:
            if route_completed == 100:
                message = "> SUCCESS: Congratulations, route finished! "
            else:
                message = "> FAILED: The actor timed out "
        else: # This should never be triggered
            return

        if self.scenario_tree.status != py_trees.common.Status.RUNNING:
            print("\n" + message)
            print("> ")
            print("> Score: ")
            print("> - Route Completed [{}]:      {}%".format(route_symbol, route_completed))
            print("> - Outside route lanes [{}]:  {}%".format(outside_symbol, outside_route_lanes))
            print("> - Collisions [{}]:           {} times".format(collision_symbol, collisions))
            print("> - Red lights run [{}]:       {} times".format(red_light_symbol, red_light))
            print("> - Stop signs run [{}]:       {} times\n".format(stop_symbol, stop_signs))

    def _tick_scenario(self, timestamp):
        """
        Run next tick of scenario and the agent and tick the world.
        """

        if self._timestamp_last_run < timestamp.elapsed_seconds:
            self._timestamp_last_run = timestamp.elapsed_seconds

            self._watchdog.update()
            # Update game time and actor information
            GameTime.on_carla_tick(timestamp)
            CarlaDataProvider.on_carla_tick()

            if self._agent is not None:
                ego_action = self._agent()

            # Tick scenario
            self.scenario_tree.tick_once()

            if self._debug_mode > 1:
                print("\n")
                py_trees.display.print_ascii_tree(
                    self.scenario_tree, show_status=True)
                sys.stdout.flush()

            if self.scenario_tree.status != py_trees.common.Status.RUNNING:
                self._running = False

            if self._challenge_mode:

                spectator = CarlaDataProvider.get_world().get_spectator()
                ego_trans = self.ego_vehicles[0].get_transform()
                spectator.set_transform(carla.Transform(ego_trans.location + carla.Location(z=50),
                                                            carla.Rotation(pitch=-90)))

            if self._agent is not None:
                self.ego_vehicles[0].apply_control(ego_action)

        if self._agent and self._running and self._watchdog.get_status():
            CarlaDataProvider.get_world().tick()

    def get_running_status(self):
        """
        returns:
           bool: False if watchdog exception occured, True otherwise
        """
        return self._watchdog.get_status()

    def stop_scenario(self):
        """
        This function triggers a proper termination of a scenario
        """

        if self.scenario is not None:
            self.scenario.terminate()

        if self._agent is not None:
            self._agent.cleanup()
            self._agent = None

        CarlaDataProvider.cleanup()
