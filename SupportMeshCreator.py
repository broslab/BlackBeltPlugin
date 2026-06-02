# Copyright (c) 2018 fieldOfView
# The Blackbelt plugin is released under the terms of the LGPLv3 or higher.
# Adapted for Cura 4.10 by broslab and ClaudeCode
# Uses Cura's built-in trimesh. outline() replaced with manual edge detection
# to avoid dependency on 'rtree' module not available in Cura.

import numpy
import math
import trimesh

from UM.Application import Application
from UM.Logger import Logger
from UM.Mesh.MeshData import MeshData, calculateNormalsFromIndexedVertices

from UM.i18n import i18nCatalog
catalog = i18nCatalog("cura")


def _get_boundary_edges(faces):
    """Return boundary (outline) edge vertex index pairs from a face array.
    Boundary edges appear exactly once across all faces."""
    # Build a list of all edges (each face has 3 edges)
    edges = numpy.concatenate([
        faces[:, [0, 1]],
        faces[:, [1, 2]],
        faces[:, [2, 0]],
    ], axis=0)
    # Sort each edge so (a,b) == (b,a)
    edges = numpy.sort(edges, axis=1)
    # Find edges that appear exactly once (boundary edges)
    unique, counts = numpy.unique(edges, axis=0, return_counts=True)
    return unique[counts == 1]


def _boundary_edges_to_loops(boundary_edges, vertices):
    """Convert boundary edges into ordered loops of vertex indices."""
    if len(boundary_edges) == 0:
        return []

    # Build adjacency map
    adjacency = {}
    for e in boundary_edges:
        a, b = int(e[0]), int(e[1])
        adjacency.setdefault(a, []).append(b)
        adjacency.setdefault(b, []).append(a)

    visited_edges = set()
    loops = []

    for start in adjacency:
        if all(
            (min(start, nb), max(start, nb)) in visited_edges
            for nb in adjacency[start]
        ):
            continue

        loop = [start]
        current = start
        prev = None

        while True:
            neighbours = adjacency.get(current, [])
            next_v = None
            for nb in neighbours:
                edge_key = (min(current, nb), max(current, nb))
                if edge_key not in visited_edges:
                    next_v = nb
                    visited_edges.add(edge_key)
                    break
            if next_v is None or next_v == start:
                break
            loop.append(next_v)
            prev = current
            current = next_v

        if len(loop) >= 2:
            loops.append(loop)

    return loops


class SupportMeshCreator():
    def __init__(self,
                 support_angle=None,
                 filter_upwards_facing_faces=True,
                 down_vector=numpy.array([0, -1, 0]),
                 bottom_cut_off=0,
                 minimum_island_area=0):
        self._support_angle = support_angle
        if self._support_angle is None:
            global_container_stack = Application.getInstance().getGlobalContainerStack()
            if global_container_stack:
                support_extruder_nr = global_container_stack.getExtruderPositionValueWithDefault("support_extruder_nr")
                support_angle_stack = Application.getInstance().getExtruderManager().getExtruderStack(support_extruder_nr)
                self._support_angle = support_angle_stack.getProperty("support_angle", "value")
            else:
                self._support_angle = 50

        self._filter_upwards_facing_faces = filter_upwards_facing_faces
        self._minimum_island_area = minimum_island_area
        self._down_vector = down_vector
        self._bottom_cut_off = bottom_cut_off

    def createSupportMeshForNode(self, node):
        node_name = node.getName()
        mesh_data = node.getMeshData().getTransformed(node.getWorldTransformation())

        node_vertices = mesh_data.getVertices()
        node_indices = mesh_data.getIndices()
        if node_indices is None:
            node_indices = numpy.arange(len(node_vertices)).reshape(-1, 3)

        support_mesh = self.createSupportMesh(node_name, node_vertices, node_indices)
        if support_mesh is not None:
            return self._toMeshData(support_mesh)

    def createSupportMesh(self, node_name, node_vertices, node_indices):
        tri_mesh = trimesh.base.Trimesh(vertices=node_vertices, faces=node_indices)
        tri_mesh.fix_normals()

        cos_support_angle = math.cos(math.radians(90 - self._support_angle))

        cos_angle_between_normal_down = numpy.dot(tri_mesh.face_normals, self._down_vector)
        faces_needing_support = numpy.argwhere(
            cos_angle_between_normal_down >= cos_support_angle).flatten()

        if len(faces_needing_support) == 0 and self._filter_upwards_facing_faces:
            faces_facing_down = numpy.argwhere(tri_mesh.face_normals[:, 1] < 0)
            faces_needing_support = numpy.intersect1d(faces_facing_down, faces_needing_support)

        if len(faces_needing_support) == 0:
            Logger.log("d", "Node %s doesn't need support" % node_name)
            return None

        roof_indices = node_indices[faces_needing_support]

        non_bottom_indices = numpy.where(
            numpy.any(node_vertices[roof_indices].take(1, axis=2) > self._bottom_cut_off, axis=1)
        )[0].flatten()
        roof_indices = roof_indices[non_bottom_indices]

        if len(roof_indices) == 0:
            Logger.log("d", "Node %s doesn't need support" % node_name)
            return None

        roof = trimesh.base.Trimesh(vertices=node_vertices, faces=roof_indices)
        roof.remove_unreferenced_vertices()
        roof.process()

        if self._minimum_island_area > 0:
            scale_matrix = trimesh.transformations.scale_matrix(0, direction=[0, 1, 0])
            roof_elements = roof.split(only_watertight=False)
            filtered_elements = []
            for roof_element in roof_elements:
                xy_element = roof_element.copy()
                xy_element.apply_transform(scale_matrix)
                if xy_element.area >= self._minimum_island_area:
                    filtered_elements.append(roof_element)
            if filtered_elements:
                roof = trimesh.util.concatenate(filtered_elements)
            else:
                roof = trimesh.base.Trimesh()

        num_roof_vertices = len(roof.vertices)
        if num_roof_vertices == 0:
            Logger.log("d", "All surfaces of node %s that need support are smaller than %f" % (
                node_name, self._minimum_island_area))
            return None

        # Build outline manually to avoid dependency on 'rtree'
        connecting_faces = []
        boundary_edges = _get_boundary_edges(roof.faces)
        loops = _boundary_edges_to_loops(boundary_edges, roof.vertices)

        for loop in loops:
            num_outline_vertices = len(loop)
            for i in range(num_outline_vertices):
                a = loop[i]
                b = loop[(i + 1) % num_outline_vertices]
                connecting_faces.append([a, b + num_roof_vertices, a + num_roof_vertices])
                connecting_faces.append([a, b, b + num_roof_vertices])

        if not connecting_faces:
            Logger.log("w", "BeltPlugin: No boundary edges found for support mesh of node %s" % node_name)
            return None

        support_vertices = numpy.concatenate((roof.vertices, roof.vertices * [1, 0, 1]))
        support_faces = numpy.concatenate((
            roof.faces,
            roof.faces + len(roof.vertices),
            numpy.array(connecting_faces, dtype=numpy.int32)
        ))

        support_mesh = trimesh.base.Trimesh(vertices=support_vertices, faces=support_faces)
        support_mesh.fix_normals()
        return support_mesh

    def _toMeshData(self, tri_node: trimesh.base.Trimesh) -> MeshData:
        tri_faces = tri_node.faces
        tri_vertices = tri_node.vertices

        indices = []
        vertices = []
        index_count = 0
        face_count = 0

        for tri_face in tri_faces:
            face = []
            for tri_index in tri_face:
                vertices.append(tri_vertices[tri_index])
                face.append(index_count)
                index_count += 1
            indices.append(face)
            face_count += 1

        vertices = numpy.asarray(vertices, dtype=numpy.float32)
        indices = numpy.asarray(indices, dtype=numpy.int32)
        normals = calculateNormalsFromIndexedVertices(vertices, indices, face_count)

        return MeshData(vertices=vertices, indices=indices, normals=normals)
