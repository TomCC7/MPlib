from __future__ import annotations

import warnings
from typing import Literal, Optional, Sequence, Union

import numpy as np
from sapien import Entity, Scene
from sapien.physx import (
    PhysxArticulation,
    PhysxArticulationLinkComponent,
    PhysxCollisionShapeBox,
    PhysxCollisionShapeCapsule,
    PhysxCollisionShapeConvexMesh,
    PhysxCollisionShapeCylinder,
    PhysxCollisionShapePlane,
    PhysxCollisionShapeSphere,
    PhysxCollisionShapeTriangleMesh,
    PhysxRigidBaseComponent,
)
from transforms3d.euler import euler2quat

from .. import ArticulatedModel, PlanningWorld, Pose
from ..collision_detection import (
    AllowedCollision,
    AllowedCollisionMatrix,
    WorldCollisionResult,
    WorldDistanceResult,
)
from ..collision_detection.fcl import (
    Box,
    BVHModel,
    Capsule,
    CollisionGeometry,
    CollisionObject,
    Convex,
    Cylinder,
    FCLModel,
    FCLObject,
    Halfspace,
    Sphere,
    collide,
    distance,
)
from ..planner import Planner
from ..planning.ompl import OMPLPlanner
from .srdf_exporter import export_srdf
from .urdf_exporter import export_kinematic_chain_urdf

YELLOW_COLOR = "\033[93m"
RESET_COLOR = "\033[0m"


# TODO: link names?
def convert_object_name(obj: Union[PhysxArticulation, Entity]) -> str:
    """
    Constructs a unique name for the corresponding mplib object.
    This is necessary because mplib objects assume unique names.

    :param obj: a SAPIEN object
    :return: the unique mplib object name
    """
    if isinstance(obj, PhysxArticulation):
        return f"{obj.name}_{obj.root.entity.per_scene_id}"
    elif isinstance(obj, Entity):
        return f"{obj.name}_{obj.per_scene_id}"
    else:
        raise NotImplementedError(f"Unknown SAPIEN object type {type(obj)}")


class SapienPlanningWorld(PlanningWorld):
    def __init__(
        self,
        sim_scene: Scene,
        planned_articulations: list[PhysxArticulation] = [],  # noqa: B006
    ):
        """
        Creates an mplib.PlanningWorld from a sapien.Scene.

        :param planned_articulations: list of planned articulations.
        """
        super().__init__([])
        self._sim_scene = sim_scene

        articulations: list[PhysxArticulation] = sim_scene.get_all_articulations()
        actors: list[Entity] = sim_scene.get_all_actors()

        for articulation in articulations:
            urdf_str = export_kinematic_chain_urdf(articulation)
            srdf_str = export_srdf(articulation)

            # Convert all links to FCLObject
            collision_links = [
                fcl_obj
                for link in articulation.links
                if (fcl_obj := self.convert_physx_component(link)) is not None
            ]

            articulated_model = ArticulatedModel.create_from_urdf_string(
                urdf_str,
                srdf_str,
                collision_links=collision_links,
                gravity=sim_scene.get_physx_system().config.gravity,  # type: ignore
                link_names=[link.name for link in articulation.links],
                joint_names=[j.name for j in articulation.active_joints],
                verbose=False,
            )
            articulated_model.set_base_pose(articulation.root_pose)  # type: ignore
            articulated_model.set_qpos(
                articulation.qpos,  # type: ignore
                full=True,
            )  # update qpos
            self.add_articulation(articulated_model)

        for articulation in planned_articulations:
            self.set_articulation_planned(convert_object_name(articulation), True)

        for entity in actors:
            component = entity.find_component_by_type(PhysxRigidBaseComponent)
            assert component is not None, (
                f"No PhysxRigidBaseComponent found in {entity.name}: "
                f"{entity.components=}"
            )

            # Convert collision shapes at current global pose
            if (fcl_obj := self.convert_physx_component(component)) is not None:  # type: ignore
                self.add_object(fcl_obj)

    def update_from_simulation(self, *, update_attached_object: bool = True) -> None:
        """
        Updates PlanningWorld's articulations/objects pose with current Scene state.
        Note that shape's local_pose is not updated.
        If those are changed, please recreate a new SapienPlanningWorld instance.

        :param update_attached_object: whether to update the attached pose of
            all attached objects
        """
        for articulation in self._sim_scene.get_all_articulations():
            if art := self.get_articulation(convert_object_name(articulation)):
                art.set_base_pose(articulation.root_pose)  # type: ignore
                # set_qpos to update poses
                art.set_qpos(articulation.qpos, full=True)  # type: ignore
            else:
                raise RuntimeError(
                    f"Articulation {articulation.name} not found in PlanningWorld! "
                    "The scene might have changed since last update."
                )

        for entity in self._sim_scene.get_all_actors():
            object_name = convert_object_name(entity)

            # If entity is an attached object
            if attached_body := self.get_attached_object(object_name):
                if update_attached_object:  # update attached pose
                    attached_body.pose = (
                        attached_body.get_attached_link_global_pose().inv()
                        * entity.pose  # type: ignore
                    )
                attached_body.update_pose()
            elif fcl_obj := self.get_object(object_name):
                # Overwrite the object
                self.add_object(
                    FCLObject(
                        object_name,
                        entity.pose,  # type: ignore
                        fcl_obj.shapes,
                        fcl_obj.shape_poses,
                    )
                )
            elif (
                len(
                    entity.find_component_by_type(
                        PhysxRigidBaseComponent
                    ).collision_shapes  # type: ignore
                )
                > 0
            ):
                warnings.warn(
                    YELLOW_COLOR + f"Entity {entity.name} not found in PlanningWorld! "
                    "The scene might have changed since last update. "
                    "Use PlanningWorld.add_object() to add the object." + RESET_COLOR,
                    stacklevel=2,
                )

    def check_collision_between(
        self,
        obj_A: Union[PhysxArticulation, Entity],
        obj_B: Union[PhysxArticulation, Entity],
        *,
        acm: AllowedCollisionMatrix = AllowedCollisionMatrix(),  # noqa: B008
    ) -> list[WorldCollisionResult]:
        """
        Check collision between two objects,
        which can either be a PhysxArticulation or an Entity.

        :param obj_A: object A to check for collision.
        :param obj_B: object B to check for collision.
        :param acm: allowed collision matrix.
        :return: a list of WorldCollisionResult. Empty if there's no collision.
        """
        col_obj_A = self._get_collision_obj(obj_A)
        col_obj_B = self._get_collision_obj(obj_B)

        if isinstance(obj_A, PhysxArticulation):  # A is articulation, B is anything
            assert isinstance(col_obj_A, FCLModel), f"Wrong type: {type(col_obj_A)}"
            return col_obj_A.check_collision_with(col_obj_B, acm=acm)
        elif isinstance(obj_B, PhysxArticulation):  # A is object, B is articulation
            assert isinstance(col_obj_B, FCLModel), f"Wrong type: {type(col_obj_B)}"
            return col_obj_B.check_collision_with(col_obj_A, acm=acm)
        elif isinstance(obj_B, Entity):  # A is object, B is object
            assert isinstance(col_obj_A, FCLObject) and isinstance(
                col_obj_B, FCLObject
            ), f"Wrong type: col_obj_A={type(col_obj_A)}, col_obj_B={type(col_obj_B)}"
            if (
                acm_type := acm.get_allowed_collision(col_obj_A.name, col_obj_B.name)
            ) is None or acm_type == AllowedCollision.NEVER:
                result = collide(col_obj_A, col_obj_B)
                if result.is_collision():
                    return [
                        WorldCollisionResult(
                            result,
                            "object_object",
                            col_obj_A.name,
                            col_obj_B.name,
                            col_obj_A.name,
                            col_obj_B.name,
                        )
                    ]
            return []
        else:
            raise NotImplementedError(f"obj_A={obj_A}, obj_B={obj_B}")

    def distance_between(
        self,
        obj_A: Union[PhysxArticulation, Entity],
        obj_B: Union[PhysxArticulation, Entity],
        *,
        acm: AllowedCollisionMatrix = AllowedCollisionMatrix(),  # noqa: B008
        return_distance_only: bool = True,
    ) -> Union[WorldDistanceResult, float]:
        """
        Check distance-to-collision between two objects,
        which can either be a PhysxArticulation or an Entity.

        :param obj_A: object A to check for collision.
        :param obj_B: object B to check for collision.
        :param acm: allowed collision matrix.
        :param return_distance_only: if True, return distance only.
        :return: a WorldDistanceResult or a float if return_distance_only==True.
        """
        col_obj_A = self._get_collision_obj(obj_A)
        col_obj_B = self._get_collision_obj(obj_B)
        ret = WorldDistanceResult()

        if isinstance(obj_A, PhysxArticulation):  # A is articulation, B is anything
            assert isinstance(col_obj_A, FCLModel), f"Wrong type: {type(col_obj_A)}"
            ret = col_obj_A.distance_with(col_obj_B, acm=acm)
        elif isinstance(obj_B, PhysxArticulation):  # A is object, B is articulation
            assert isinstance(col_obj_B, FCLModel), f"Wrong type: {type(col_obj_B)}"
            ret = col_obj_B.distance_with(col_obj_A, acm=acm)
        elif isinstance(obj_B, Entity):  # A is object, B is object
            assert isinstance(col_obj_A, FCLObject) and isinstance(
                col_obj_B, FCLObject
            ), f"Wrong type: col_obj_A={type(col_obj_A)}, col_obj_B={type(col_obj_B)}"
            if (
                acm_type := acm.get_allowed_collision(col_obj_A.name, col_obj_B.name)
            ) is None or acm_type == AllowedCollision.NEVER:
                result = distance(col_obj_A, col_obj_B)
                ret = WorldDistanceResult(
                    result,
                    result.min_distance,
                    "object_object",
                    col_obj_A.name,
                    col_obj_B.name,
                    col_obj_A.name,
                    col_obj_B.name,
                )
        else:
            raise NotImplementedError(f"obj_A={obj_A}, obj_B={obj_B}")

        return ret.min_distance if return_distance_only else ret

    def _get_collision_obj(
        self,
        obj: Union[PhysxArticulation, Entity],
    ) -> Union[FCLModel, FCLObject, None]:
        """Helper function to get mplib collision object from sapien object"""
        if isinstance(obj, PhysxArticulation) and (
            articulation := self.get_articulation(convert_object_name(obj))
        ):
            return articulation.get_fcl_model()
        elif isinstance(obj, Entity) and (
            fcl_obj := self.get_object(convert_object_name(obj))
        ):
            return fcl_obj
        else:
            raise RuntimeError(
                f"Unknown SAPIEN object type: {type(obj)} or "
                f"Object {obj.name} not found in PlanningWorld "
                "(The scene might have changed since last update)"
            )

    @staticmethod
    def convert_physx_component(
        comp: PhysxRigidBaseComponent,
    ) -> Union[FCLObject, None]:
        """
        Converts a SAPIEN physx.PhysxRigidBaseComponent to an FCLObject.
        All shapes in the returned FCLObject are already set at their world poses.

        :param comp: a SAPIEN physx.PhysxRigidBaseComponent.
        :return: an FCLObject containing all collision shapes in the Physx component.
            If the component has no collision shapes, return ``None``.
        """
        shapes: list[CollisionObject] = []
        shape_poses: list[Pose] = []
        for shape in comp.collision_shapes:
            shape_poses.append(Pose(shape.local_pose))  # type: ignore

            if isinstance(shape, PhysxCollisionShapeBox):
                c_geom = Box(side=shape.half_size * 2)
            elif isinstance(shape, PhysxCollisionShapeCapsule):
                c_geom = Capsule(radius=shape.radius, lz=shape.half_length * 2)
                # NOTE: physx Capsule has x-axis along capsule height
                # FCL Capsule has z-axis along capsule height
                shape_poses[-1] *= Pose(q=euler2quat(0, np.pi / 2, 0))
            elif isinstance(shape, PhysxCollisionShapeConvexMesh):
                scaled_vertices = shape.vertices * np.expand_dims(shape.scale, axis=0)
                c_geom = Convex(vertices=scaled_vertices, faces=shape.triangles)
            elif isinstance(shape, PhysxCollisionShapeCylinder):
                c_geom = Cylinder(radius=shape.radius, lz=shape.half_length * 2)
                # NOTE: physx Cylinder has x-axis along cylinder height
                # FCL Cylinder has z-axis along cylinder height
                shape_poses[-1] *= Pose(q=euler2quat(0, np.pi / 2, 0))
            elif isinstance(shape, PhysxCollisionShapePlane):
                # PhysxCollisionShapePlane are actually a halfspace
                # https://nvidia-omniverse.github.io/PhysX/physx/5.3.1/docs/Geometry.html#planes
                # PxPlane's Pose determines its normal and offert (normal is +x)
                n = shape_poses[-1].to_transformation_matrix()[:3, 0]
                d = n.dot(shape_poses[-1].p)
                c_geom = Halfspace(n=n, d=d)
                shape_poses[-1] = Pose()
            elif isinstance(shape, PhysxCollisionShapeSphere):
                c_geom = Sphere(radius=shape.radius)
            elif isinstance(shape, PhysxCollisionShapeTriangleMesh):
                c_geom = BVHModel()
                c_geom.begin_model()
                c_geom.add_sub_model(vertices=shape.vertices, faces=shape.triangles)  # type: ignore
                c_geom.end_model()
            else:
                raise TypeError(f"Unknown shape type: {type(shape)}")
            shapes.append(CollisionObject(c_geom))

        if len(shapes) == 0:
            return None

        return FCLObject(
            comp.name
            if isinstance(comp, PhysxArticulationLinkComponent)
            else convert_object_name(comp.entity),
            comp.entity.pose,  # type: ignore
            shapes,
            shape_poses,
        )

    # ----- Overloaded PlanningWorld methods to accept SAPIEN objects ----- #
    def is_articulation_planned(
        self, articulation: Union[PhysxArticulation, str]
    ) -> bool:  # type: ignore
        """
        Check whether the given articulation is being planned

        :param articulation: the articulation (or its name) to check
        :return: ``True`` if the articulation is being planned, ``False`` otherwise.

        .. raw:: html

            <details>
            <summary><a>Overloaded
            <code class="docutils literal notranslate">
            <span class="pre">PlanningWorld.is_articulation_planned()</span>
            </code>
            method</a></summary>
        .. automethod:: mplib.PlanningWorld.is_articulation_planned
           :no-index:
        .. raw:: html
            </details>
        """
        if isinstance(articulation, PhysxArticulation):
            articulation = convert_object_name(articulation)
        return super().is_articulation_planned(articulation)

    def set_articulation_planned(  # type: ignore
        self, articulation: Union[PhysxArticulation, str], planned: bool
    ) -> None:
        """
        Sets the given articulation as being planned or not

        :param articulation: the articulation (or its name)
        :param planned: whether the articulation should be planned

        .. raw:: html

            <details>
            <summary><a>Overloaded
            <code class="docutils literal notranslate">
            <span class="pre">PlanningWorld.set_articulation_planned()</span>
            </code>
            method</a></summary>
        .. automethod:: mplib.PlanningWorld.set_articulation_planned
           :no-index:
        .. raw:: html
            </details>
        """
        if isinstance(articulation, PhysxArticulation):
            articulation = convert_object_name(articulation)
        super().set_articulation_planned(articulation, planned)

    def has_object(self, obj: Union[Entity, str]) -> bool:
        """
        Check whether the given non-articulated object exists.

        :param obj: the object (or its name) to check

        :return: ``True`` if the object exists, ``False`` otherwise.

        .. raw:: html

            <details>
            <summary><a>Overloaded
            <code class="docutils literal notranslate">
            <span class="pre">PlanningWorld.has_object()</span>
            </code>
            method</a></summary>
        .. automethod:: mplib.PlanningWorld.has_object
           :no-index:
        .. raw:: html
            </details>
        """
        if isinstance(obj, Entity):
            obj = convert_object_name(obj)
        return super().has_object(obj)

    def add_object(self, obj: Union[FCLObject, Entity]) -> None:
        """
        Adds a non-articulated object to the PlanningWorld.

        :param obj: the non-articulated object to add

        .. raw:: html

            <details>
            <summary><a>Overloaded
            <code class="docutils literal notranslate">
            <span class="pre">PlanningWorld.add_object()</span>
            </code>
            method</a></summary>
        .. automethod:: mplib.PlanningWorld.add_object
           :no-index:
        .. raw:: html
            </details>
        """
        if isinstance(obj, Entity):
            component = obj.find_component_by_type(PhysxRigidBaseComponent)
            assert component is not None, (
                f"No PhysxRigidBaseComponent found in {obj.name}: " f"{obj.components=}"
            )

            # Convert collision shapes at current global pose
            if (fcl_obj := self.convert_physx_component(component)) is not None:  # type: ignore
                self.add_object(fcl_obj)
        else:
            super().add_object(obj)

    def remove_object(self, obj: Union[Entity, str]) -> None:
        """
        Removes a non-articulated object from the PlanningWorld.

        :param obj: the non-articulated object (or its name) to remove

        .. raw:: html

            <details>
            <summary><a>Overloaded
            <code class="docutils literal notranslate">
            <span class="pre">PlanningWorld.remove_object()</span>
            </code>
            method</a></summary>
        .. automethod:: mplib.PlanningWorld.remove_object
           :no-index:
        .. raw:: html
            </details>
        """
        if isinstance(obj, Entity):
            obj = convert_object_name(obj)
        super().remove_object(obj)

    def is_object_attached(self, obj: Union[Entity, str]) -> bool:  # type: ignore
        """
        Check whether the given non-articulated object is attached

        :param obj: the non-articulated object (or its name) to check
        :return: ``True`` if the articulation is attached, ``False`` otherwise.

        .. raw:: html

            <details>
            <summary><a>Overloaded
            <code class="docutils literal notranslate">
            <span class="pre">PlanningWorld.is_object_attached()</span>
            </code>
            method</a></summary>
        .. automethod:: mplib.PlanningWorld.is_object_attached
           :no-index:
        .. raw:: html
            </details>
        """
        if isinstance(obj, Entity):
            obj = convert_object_name(obj)
        return super().is_object_attached(obj)

    def attach_object(  # type: ignore
        self,
        obj: Union[Entity, str],
        articulation: Union[PhysxArticulation, str],
        link: Union[PhysxArticulationLinkComponent, int],
        pose: Optional[Pose] = None,
        *,
        touch_links: Optional[list[Union[PhysxArticulationLinkComponent, str]]] = None,
        obj_geom: Optional[CollisionGeometry] = None,
    ) -> None:
        """
        Attaches given non-articulated object to the specified link of articulation.

        Updates ``acm_`` to allow collisions between attached object and touch_links.

        :param obj: the non-articulated object (or its name) to attach
        :param articulation: the planned articulation (or its name) to attach to
        :param link: the link of the planned articulation (or its index) to attach to
        :param pose: attached pose (relative pose from attached link to object).
            If ``None``, attach the object at its current pose.
        :param touch_links: links (or their names) that the attached object touches.
            When ``None``,

            * if the object is not currently attached, touch_links are set to the name
            of articulation links that collide with the object in the current state.

            * if the object is already attached, touch_links of the attached object
            is preserved and ``acm_`` remains unchanged.
        :param obj_geom: a CollisionGeometry object representing the attached object.
            If not ``None``, pose must be not ``None``.

        .. raw:: html

            <details>
            <summary><a>Overloaded
            <code class="docutils literal notranslate">
            <span class="pre">PlanningWorld.attach_object()</span>
            </code>
            methods</a></summary>
        .. automethod:: mplib.PlanningWorld.attach_object
           :no-index:
        .. raw:: html
            </details>
        """
        kwargs = {"name": obj, "art_name": articulation, "link_id": link}
        if pose is not None:
            kwargs["pose"] = pose
        if touch_links is not None:
            kwargs["touch_links"] = [
                l.name if isinstance(l, PhysxArticulationLinkComponent) else l
                for l in touch_links  # noqa: E741
            ]
        if obj_geom is not None:
            kwargs["obj_geom"] = obj_geom

        if isinstance(obj, Entity):
            kwargs["name"] = convert_object_name(obj)
        if isinstance(articulation, PhysxArticulation):
            kwargs["art_name"] = articulation = convert_object_name(articulation)
        if isinstance(link, PhysxArticulationLinkComponent):
            kwargs["link_id"] = (
                self.get_articulation(articulation)
                .get_pinocchio_model()
                .get_link_names()
                .index(link.name)
            )

        super().attach_object(**kwargs)

    def detach_object(self, obj: Union[Entity, str], also_remove: bool = False) -> bool:  # type: ignore
        """
        Detaches the given object.

        Updates ``acm_`` to disallow collision between the object and touch_links.

        :param obj: the non-articulated object (or its name) to detach
        :param also_remove: whether to also remove the object from PlanningWorld
        :return: ``True`` if success, ``False`` if the given object is not attached.

        .. raw:: html

            <details>
            <summary><a>Overloaded
            <code class="docutils literal notranslate">
            <span class="pre">PlanningWorld.detach_object()</span>
            </code>
            method</a></summary>
        .. automethod:: mplib.PlanningWorld.detach_object
           :no-index:
        .. raw:: html
            </details>
        """
        if isinstance(obj, Entity):
            obj = convert_object_name(obj)
        return super().detach_object(obj, also_remove=also_remove)

    def attach_sphere(  # type: ignore
        self,
        radius: float,
        articulation: Union[PhysxArticulation, str],
        link: Union[PhysxArticulationLinkComponent, int],
        pose: Pose,
    ) -> None:
        """
        Attaches given sphere to specified link of articulation (auto touch_links)

        :param radius: sphere radius
        :param articulation: the planned articulation (or its name) to attach to
        :param link: the link of the planned articulation (or its index) to attach to
        :param pose: attached pose (relative pose from attached link to object)

        .. raw:: html

            <details>
            <summary><a>Overloaded
            <code class="docutils literal notranslate">
            <span class="pre">PlanningWorld.attach_sphere()</span>
            </code>
            method</a></summary>
        .. automethod:: mplib.PlanningWorld.attach_sphere
           :no-index:
        .. raw:: html
            </details>
        """
        if isinstance(articulation, PhysxArticulation):
            articulation = convert_object_name(articulation)
        if isinstance(link, PhysxArticulationLinkComponent):
            link = (
                self.get_articulation(articulation)
                .get_pinocchio_model()
                .get_link_names()
                .index(link.name)
            )
        super().attach_sphere(radius, articulation, link, pose)

    def attach_box(  # type: ignore
        self,
        size: Union[
            Sequence[float],
            np.ndarray[tuple[Literal[3], Literal[1]], np.dtype[np.floating]],
        ],
        articulation: Union[PhysxArticulation, str],
        link: Union[PhysxArticulationLinkComponent, int],
        pose: Pose,
    ) -> None:
        """
        Attaches given box to specified link of articulation (auto touch_links)

        :param size: box side length
        :param articulation: the planned articulation (or its name) to attach to
        :param link: the link of the planned articulation (or its index) to attach to
        :param pose: attached pose (relative pose from attached link to object)

        .. raw:: html

            <details>
            <summary><a>Overloaded
            <code class="docutils literal notranslate">
            <span class="pre">PlanningWorld.attach_box()</span>
            </code>
            method</a></summary>
        .. automethod:: mplib.PlanningWorld.attach_box
           :no-index:
        .. raw:: html
            </details>
        """
        if isinstance(articulation, PhysxArticulation):
            articulation = convert_object_name(articulation)
        if isinstance(link, PhysxArticulationLinkComponent):
            link = (
                self.get_articulation(articulation)
                .get_pinocchio_model()
                .get_link_names()
                .index(link.name)
            )
        super().attach_box(size, articulation, link, pose)  # type: ignore

    def attach_mesh(  # type: ignore
        self,
        mesh_path: str,
        articulation: Union[PhysxArticulation, str],
        link: Union[PhysxArticulationLinkComponent, int],
        pose: Pose,
        *,
        convex: bool = False,
    ) -> None:
        """
        Attaches given mesh to specified link of articulation (auto touch_links)

        :param mesh_path: path to a mesh file
        :param articulation: the planned articulation (or its name) to attach to
        :param link: the link of the planned articulation (or its index) to attach to
        :param pose: attached pose (relative pose from attached link to object)
        :param convex: whether to load mesh as a convex mesh. Default: ``False``.

        .. raw:: html

            <details>
            <summary><a>Overloaded
            <code class="docutils literal notranslate">
            <span class="pre">PlanningWorld.attach_mesh()</span>
            </code>
            method</a></summary>
        .. automethod:: mplib.PlanningWorld.attach_mesh
           :no-index:
        .. raw:: html
            </details>
        """
        if isinstance(articulation, PhysxArticulation):
            articulation = convert_object_name(articulation)
        if isinstance(link, PhysxArticulationLinkComponent):
            link = (
                self.get_articulation(articulation)
                .get_pinocchio_model()
                .get_link_names()
                .index(link.name)
            )
        super().attach_mesh(mesh_path, articulation, link, pose, convex=convex)

    def set_allowed_collision(
        self,
        obj1: Union[Entity, PhysxArticulationLinkComponent, str],
        obj2: Union[Entity, PhysxArticulationLinkComponent, str],
        allowed: bool,
    ) -> None:
        """
        Set allowed collision between two objects.

        :param obj1: the first object (or its name)
        :param obj2: the second object (or its name)

        .. raw:: html

            <details>
            <summary><a>Overloaded
            <code class="docutils literal notranslate">
            <span class="pre">PlanningWorld.set_allowed_collision()</span>
            </code>
            method</a></summary>
        .. automethod:: mplib.PlanningWorld.set_allowed_collision
           :no-index:
        .. raw:: html
            </details>
        """
        if isinstance(obj1, Entity):
            obj1 = convert_object_name(obj1)
        elif isinstance(obj1, PhysxArticulationLinkComponent):
            obj1 = obj1.name
        if isinstance(obj2, Entity):
            obj2 = convert_object_name(obj2)
        elif isinstance(obj2, PhysxArticulationLinkComponent):
            obj2 = obj2.name
        super().set_allowed_collision(obj1, obj2, allowed)


class SapienPlanner(Planner):
    def __init__(
        self,
        sapien_planning_world: SapienPlanningWorld,
        move_group: str,
        *,
        joint_vel_limits: Union[Sequence[float], np.ndarray, None] = None,
        joint_acc_limits: Union[Sequence[float], np.ndarray, None] = None,
    ):
        """
        Creates an mplib.planner.Planner from a SapienPlanningWorld.

        :param sapien_planning_world: a SapienPlanningWorld created from sapien.Scene
        :param move_group: name of the move group (end effector link)
        :param joint_vel_limits: joint velocity limits for time parameterization
        :param joint_acc_limits: joint acceleration limits for time parameterization
        """
        self.planning_world = sapien_planning_world
        self.acm = self.planning_world.get_allowed_collision_matrix()

        if len(planned_arts := self.planning_world.get_planned_articulations()) != 1:
            raise NotImplementedError("Must have exactly one planned articulation")
        self.robot = planned_arts[0]
        self.pinocchio_model = self.robot.get_pinocchio_model()
        self.user_link_names = self.pinocchio_model.get_link_names()
        self.user_joint_names = self.pinocchio_model.get_joint_names()

        self.joint_name_2_idx = {}
        for i, joint in enumerate(self.user_joint_names):
            self.joint_name_2_idx[joint] = i
        self.link_name_2_idx = {}
        for i, link in enumerate(self.user_link_names):
            self.link_name_2_idx[link] = i

        assert (
            move_group in self.user_link_names
        ), f"end-effector not found as one of the links in {self.user_link_names}"
        self.move_group = move_group
        self.robot.set_move_group(self.move_group)
        self.move_group_joint_indices = self.robot.get_move_group_joint_indices()

        self.joint_types = self.pinocchio_model.get_joint_types()
        self.joint_limits = np.concatenate(self.pinocchio_model.get_joint_limits())
        if joint_vel_limits is None:
            joint_vel_limits = np.ones(len(self.move_group_joint_indices))
        if joint_acc_limits is None:
            joint_acc_limits = np.ones(len(self.move_group_joint_indices))
        self.joint_vel_limits = joint_vel_limits
        self.joint_acc_limits = joint_acc_limits
        self.move_group_link_id = self.link_name_2_idx[self.move_group]

        # do a robot env collision check and warn if there is a collision
        collisions = self.planning_world.check_robot_collision()
        if len(collisions):
            for collision in collisions:
                warnings.warn(
                    YELLOW_COLOR + f"Robot's {collision.link_name1} collides with "
                    f"{collision.link_name2} of {collision.object_name2} in initial "
                    f"state. Use planner.planning_world.get_allowed_collision_matrix() "
                    f"to disable collisions if planning fails" + RESET_COLOR,
                    stacklevel=2,
                )

        assert (
            len(self.joint_vel_limits)
            == len(self.joint_acc_limits)
            == len(self.move_group_joint_indices)
            <= len(self.joint_limits)
        ), (
            "length of joint_vel_limits, joint_acc_limits, and move_group_joint_indices"
            " should equal and be <= number of total joints. "
            f"{len(self.joint_vel_limits)} == {len(self.joint_acc_limits)} "
            f"== {len(self.move_group_joint_indices)} <= {len(self.joint_limits)}"
        )

        # Mask for joints that have equivalent values (revolute joints with range > 2pi)
        self.equiv_joint_mask = [
            t.startswith("JointModelR") for t in self.joint_types
        ] & (self.joint_limits[:, 1] - self.joint_limits[:, 0] > 2 * np.pi)

        self.planner = OMPLPlanner(world=self.planning_world)

    def update_from_simulation(self, *, update_attached_object: bool = True) -> None:
        """
        Updates PlanningWorld's articulations/objects pose with current Scene state.
        Note that shape's local_pose is not updated.
        If those are changed, please recreate a new SapienPlanningWorld instance.

        Directly calls ``SapienPlanningWorld.update_from_simulation()``

        :param update_attached_object: whether to update the attached pose of
            all attached objects
        """
        self.planning_world.update_from_simulation(
            update_attached_object=update_attached_object
        )
