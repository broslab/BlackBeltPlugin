from UM.Math.Matrix import Matrix
from UM.Math.Vector import Vector
from UM.Logger import Logger
from cura.CuraApplication import CuraApplication
from cura.Scene.CuraSceneNode import CuraSceneNode
from typing import Optional


class FlavorParserPatches():

    def __init__(self, flavor_parser):
        self._flavor_parser = flavor_parser
        self.__processGCodeStream = self._flavor_parser.processGCodeStream
        self._flavor_parser.processGCodeStream = self.processGCodeStream

    # Calls original FlavorParser.processGCodeStream and untransform the parsed layers if necessary
    def processGCodeStream(self, stream: str, filename: str = "") -> Optional[CuraSceneNode]:
        try:
            scene_node = self.__processGCodeStream(stream, filename)
        except TypeError:
            # Older Cura versions don't pass filename
            try:
                scene_node = self.__processGCodeStream(stream)
            except Exception as e:
                Logger.log("e", "BeltPlugin: error in processGCodeStream: %s", str(e))
                return None
        except Exception as e:
            Logger.log("e", "BeltPlugin: error in processGCodeStream: %s", str(e))
            return None

        if not scene_node:
            return None

        try:
            root = CuraApplication.getInstance().getController().getScene().getRoot()
            root.callDecoration("calculateTransformData")
            transform = root.callDecoration("getTransformMatrix")

            if transform and transform != Matrix():
                transform_matrix = scene_node.getLocalTransformation().preMultiply(transform.getInverse())
                scene_node.setTransformation(transform_matrix)

                bounding_box = scene_node.getBoundingBox()
                if bounding_box and bounding_box.isValid():
                    scene_node.translate(Vector(0, 0, -bounding_box.back), CuraSceneNode.TransformSpace.World)

        except Exception as e:
            Logger.log("w", "BeltPlugin: could not apply belt transform to gcode preview: %s", str(e))

        return scene_node
