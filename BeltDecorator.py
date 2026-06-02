# Copyright (c) 2018 fieldOfView
# The Blackbelt plugin is released under the terms of the LGPLv3 or higher.
# Adapted for Cura 4.10 by broslab and ClaudeCode

from UM.Scene.SceneNodeDecorator import SceneNodeDecorator
from UM.Application import Application
from UM.Math.Matrix import Matrix
from UM.Logger import Logger

import math


class BeltDecorator(SceneNodeDecorator):
    """Decorator for easy access to gantry angle and transform matrix."""

    def __init__(self):
        super().__init__()
        self._gantry_angle = 0
        self._transform_matrix = Matrix()
        self._scene_front_offset = 0

    def calculateTransformData(self):
        global_stack = Application.getInstance().getGlobalContainerStack()
        if not global_stack:
            return

        self._scene_front_offset = 0
        gantry_angle = 0

        # Read directly from stack — no preferences involved
        try:
            plugin_enabled = global_stack.getProperty("belt_plugin_enable", "value")
            if plugin_enabled:
                gantry_angle = global_stack.getProperty("belt_gantry_angle", "value") or 0
        except Exception:
            gantry_angle = 0

        Logger.log("i", "BeltPlugin: gantry_angle = %s", str(gantry_angle))

        if not gantry_angle:
            self._gantry_angle = 0
            self._transform_matrix = Matrix()
            return

        self._gantry_angle = math.radians(float(gantry_angle))
        machine_depth = global_stack.getProperty("machine_depth", "value")

        matrix = Matrix()
        matrix.setColumn(1, [0, 1 / math.tan(self._gantry_angle), 1,
                             (machine_depth / 2) * (1 - math.cos(self._gantry_angle))])
        matrix.setColumn(2, [0, -1 / math.sin(self._gantry_angle), 0,
                             machine_depth / 2])
        self._transform_matrix = matrix

    def getGantryAngle(self):
        return self._gantry_angle

    def getTransformMatrix(self):
        return self._transform_matrix

    def setSceneFrontOffset(self, front_offset):
        self._scene_front_offset = front_offset

    def getSceneFrontOffset(self):
        return self._scene_front_offset
