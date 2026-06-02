# Copyright (c) 2018 fieldOfView
# The Blackbelt plugin is released under the terms of the LGPLv3 or higher.
# Adapted for Cura 4.10 by broslab

from UM.Extension import Extension
from UM.Application import Application
from UM.PluginRegistry import PluginRegistry
from UM.Settings.ContainerRegistry import ContainerRegistry
from UM.Settings.DefinitionContainer import DefinitionContainer
from UM.Settings.SettingDefinition import SettingDefinition
from UM.Logger import Logger
from UM.i18n import i18nCatalog
from UM.Resources import Resources
from UM.Backend.Backend import BackendState

from PyQt5.QtCore import QObject, pyqtSlot

from . import BeltDecorator
from . import CuraApplicationPatches
from . import PatchedCuraActions
from . import BuildVolumePatches
from . import CuraEngineBackendPatches
from . import FlavorParserPatches

from collections import OrderedDict
import math
import os.path
import re
import json

Resources.addSearchPath(os.path.join(os.path.dirname(os.path.abspath(__file__))))
i18n_catalog = i18nCatalog("belt_printer_slicing")


def _getStackValue(global_stack, key, default=None):
    """Helper: safely get a setting value from the global container stack."""
    try:
        value = global_stack.getProperty(key, "value")
        return value if value is not None else default
    except Exception:
        return default


class BeltPlugin(QObject, Extension):

    def __init__(self):
        super().__init__()
        self._plugin_path = os.path.dirname(os.path.abspath(__file__))
        self._application = Application.getInstance()
        self._settings_dict = OrderedDict()
        self._global_container_stack = None

        # Load sidebar settings definition
        try:
            def_path = os.path.join(self._plugin_path, "belt_settings.def.json")
            with open(def_path, encoding="utf-8") as f:
                self._settings_dict = json.load(f, object_pairs_hook=OrderedDict)
        except Exception as e:
            Logger.log("e", "BeltPlugin: Could not load belt_settings.def.json: %s", str(e))

        # Load translations
        self._translations = {}
        try:
            tr_path = os.path.join(self._plugin_path, "belt_translations.json")
            with open(tr_path, encoding="utf-8") as f:
                self._translations = json.load(f)
        except Exception as e:
            Logger.log("w", "BeltPlugin: Could not load belt_translations.json: %s", str(e))

        # Detect Cura language
        self._locale = "en"
        try:
            locale_str = self._application.getPreferences().getValue("general/language") or "en"
            self._locale = locale_str
        except Exception:
            pass

        # Inject settings into sidebar on container load
        ContainerRegistry.getInstance().containerLoadComplete.connect(self._onContainerLoadComplete)
        self._application.initializationFinished.connect(self._onInitializationFinished)

        self._scene_root = self._application.getController().getScene().getRoot()
        self._scene_root.addDecorator(BeltDecorator.BeltDecorator())

        self._application.getOutputDeviceManager().writeStarted.connect(self._filterGcode)
        self._application.pluginsLoaded.connect(self._onPluginsLoaded)

    # ------------------------------------------------------------------ #
    #  Sidebar settings injection                                          #
    # ------------------------------------------------------------------ #

    def _tr(self, text):
        """Translate text using belt_translations.json for the current Cura locale."""
        locale_translations = self._translations.get(self._locale, {})
        return locale_translations.get(text, text)

    def _onContainerLoadComplete(self, container_id):
        if not self._settings_dict:
            return
        if not ContainerRegistry.getInstance().isLoaded(container_id):
            return
        try:
            container = ContainerRegistry.getInstance().findContainers(id=container_id)[0]
        except IndexError:
            return
        if not isinstance(container, DefinitionContainer):
            return
        if container.getMetaDataEntry("type") == "extruder":
            return
        if container.findDefinitions(key="belt_printer_category"):
            return

        category = SettingDefinition("belt_printer_category", container, None, None)
        category.deserialize({
            "label": "Belt Printer",
            "description": "Settings for belt printer post-processing",
            "type": "category",
            "icon": "plugin"
        })
        container.addDefinition(category)

        try:
            category = container.findDefinitions(key="belt_printer_category")[0]
        except IndexError:
            Logger.log("e", "BeltPlugin: Could not find belt_printer_category after adding")
            return

        for setting_key, setting_data in self._settings_dict.items():
            # Apply translation to label and description
            translated_data = dict(setting_data)
            if "label" in translated_data:
                translated_data["label"] = self._tr(translated_data["label"])
            if "description" in translated_data:
                translated_data["description"] = self._tr(translated_data["description"])
            setting_def = SettingDefinition(setting_key, container, category, None)
            setting_def.deserialize(translated_data)
            category._children.append(setting_def)
            container._definition_cache[setting_key] = setting_def
            for child in setting_def.children:
                container._definition_cache[child.key] = child

        container._updateRelations(category)

        # Move belt category to the top of the settings list
        try:
            belt_def = container._definitions[-1]
            if belt_def.key == "belt_printer_category":
                container._definitions.pop(-1)
                container._definitions.insert(0, belt_def)
        except Exception as e:
            Logger.log("w", "BeltPlugin: Could not reorder category: %s", str(e))

        Logger.log("d", "BeltPlugin: Injected settings into '%s'", container_id)

    def _onInitializationFinished(self):
        # Make belt category visible in settings panel
        try:
            preferences = self._application.getPreferences()
            visible = preferences.getValue("general/visible_settings") or ""
            if "belt_printer_category" not in visible:
                preferences.setValue("general/visible_settings",
                                     visible + ";belt_printer_category")
        except Exception as e:
            Logger.log("w", "BeltPlugin: Could not set visibility: %s", str(e))

        self._application.getMachineManager().globalContainerChanged.connect(
            self._onGlobalContainerChanged)
        self._onGlobalContainerChanged()

    # ------------------------------------------------------------------ #
    #  Machine change handling                                             #
    # ------------------------------------------------------------------ #

    def _onGlobalContainerChanged(self):
        self._global_container_stack = self._application.getGlobalContainerStack()
        self._adjustLayerViewNozzle()

    # ------------------------------------------------------------------ #
    #  Plugin / engine lifecycle                                           #
    # ------------------------------------------------------------------ #

    def _onPluginsLoaded(self):
        self._application.engineCreatedSignal.connect(self._onEngineCreated)
        self._application.getController().activeViewChanged.connect(self._onActiveViewChanged)

    def _onEngineCreated(self):
        self._cura_application_patches = CuraApplicationPatches.CuraApplicationPatches(self._application)
        self._build_volume_patches = BuildVolumePatches.BuildVolumePatches(self._application.getBuildVolume())
        self._cura_engine_backend_patches = CuraEngineBackendPatches.CuraEngineBackendPatches(self._application.getBackend())
        self._application._cura_actions = PatchedCuraActions.PatchedCuraActions()
        self._application._qml_engine.rootContext().setContextProperty("CuraActions", self._application._cura_actions)
        self._application.getBackend().slicingStarted.connect(self._onSlicingStarted)

        gcode_reader_plugin = PluginRegistry.getInstance().getPluginObject("GCodeReader")
        self._flavor_parser_patches = {}
        if gcode_reader_plugin:
            for (parser_name, parser_object) in gcode_reader_plugin._flavor_readers_dict.items():
                self._flavor_parser_patches[parser_name] = FlavorParserPatches.FlavorParserPatches(parser_object)

    def _onSlicingStarted(self):
        self._scene_root.callDecoration("calculateTransformData")

    def _onActiveViewChanged(self):
        self._adjustLayerViewNozzle()

    def _adjustLayerViewNozzle(self):
        if not self._global_container_stack:
            return
        view = self._application.getController().getActiveView()
        if view and view.getPluginId() == "SimulationView":
            gantry_angle = _getStackValue(self._global_container_stack, "belt_gantry_angle", 0)
            plugin_enabled = _getStackValue(self._global_container_stack, "belt_plugin_enable", False)
            if plugin_enabled and gantry_angle and float(gantry_angle) > 0:
                view.getNozzleNode().setParent(None)
            else:
                view.getNozzleNode().setParent(
                    self._application.getController().getScene().getRoot())

    # ------------------------------------------------------------------ #
    #  G-code post-processing                                              #
    # ------------------------------------------------------------------ #

    def _filterGcode(self, output_device):
        global_stack = self._application.getGlobalContainerStack()
        if not global_stack:
            return

        # Read all settings directly from stack
        if not _getStackValue(global_stack, "belt_plugin_enable", False):
            return

        scene = self._application.getController().getScene()
        gcode_dict = getattr(scene, "gcode_dict", {})
        if not gcode_dict:
            Logger.log("w", "BeltPlugin: Scene has no gcode to process")
            return

        gantry_angle     = float(_getStackValue(global_stack, "belt_gantry_angle", 45))
        z_offset_gap     = float(_getStackValue(global_stack, "belt_z_offset_gap", 0.25))
        repetitions      = int(_getStackValue(global_stack, "belt_repetitions", 1))
        rep_distance     = float(_getStackValue(global_stack, "belt_repetitions_distance", 300))
        wall_enabled     = bool(_getStackValue(global_stack, "belt_wall_enabled", False))
        wall_speed       = float(_getStackValue(global_stack, "belt_wall_speed", 600.0))
        wall_flow        = float(_getStackValue(global_stack, "belt_wall_flow", 1.0))
        fans_enabled     = bool(_getStackValue(global_stack, "belt_secondary_fans_enabled", False))
        fans_speed       = float(_getStackValue(global_stack, "belt_secondary_fans_speed", 1.0))

        repetitions_gcode = (
            "\nG92 E0 ; Set Extruder to zero\nG1 E-4 F3900 ; Retract 4mm at 65mm/s\n"
            "G92 Z0 ; Set Belt to zero\nG1 Z{belt_repetitions_distance} ; Advance belt\n"
            "G92 Z0 ; Set Belt to zero again\n\n"
            "M107 ; Start with the fan off\nG0 X170 ; Move X to the center\n"
            "G1 Y1 ; Move y to the belt\nG1 E0 ; Move extruder back to 0\n"
            "G92 E-5 ; Add 5mm restart distance\n\n"
        )

        wall_line_width_0 = float(global_stack.extruders["0"].getProperty("wall_line_width_0", "value"))
        xy_offset         = float(global_stack.extruders["0"].getProperty("xy_offset", "value"))
        belt_z_offset     = round(
            (wall_line_width_0 / 2.0) - (z_offset_gap / math.sin(math.radians(gantry_angle))) - xy_offset, 4)

        minimum_y = wall_line_width_0 * 0.6

        dict_changed = False

        for plate_id in gcode_dict:
            gcode_list = gcode_dict[plate_id]
            if not gcode_list:
                continue
            if ";BELTPROCESSED" in gcode_list[0]:
                Logger.log("d", "BeltPlugin: Already post processed, skipping")
                continue

            # Bed temperature fix
            init_bed_temp  = global_stack.getProperty("material_bed_temperature_layer_0", "value")
            layer_bed_temp = global_stack.getProperty("material_bed_temperature", "value")

            temp_disable = re.compile(r"M140\s+S0\b")
            for i, layer in enumerate(gcode_list):
                gcode_list[i] = re.sub(temp_disable, "----DISABLE BED---140 S0", layer)

            temp_set = re.compile(r"M140\s+S(\d*\.?\d*)")
            for i, layer in enumerate(gcode_list):
                t = init_bed_temp if i == 0 else layer_bed_temp
                gcode_list[i] = re.sub(temp_set, lambda m, t=t: "M140 S%d" % int(t), layer)

            temp_restore = re.compile(r"----DISABLE BED---140 S0\b")
            for i, layer in enumerate(gcode_list):
                gcode_list[i] = re.sub(temp_restore, "M140 S0", layer)

            # Z offset substitution
            gcode_list[1]  = gcode_list[1].replace("{belt_z_offset}", str(belt_z_offset))
            gcode_list[-1] = gcode_list[-1].replace("{belt_z_offset}", str(belt_z_offset))

            # Secondary fans
            if fans_enabled:
                fan_regex = re.compile(r"M106\s+S(\d*\.?\d*)")
                for i, layer in enumerate(gcode_list):
                    gcode_list[i] = re.sub(
                        fan_regex,
                        lambda m: "M106 P1 S%d\nM106 S%s" % (
                            int(min(255, float(m.group(1)) * fans_speed)), m.group(1)),
                        layer)

            # Belt wall adjustment
            if wall_enabled:
                y = last_y = e = last_e = f = None
                speed_re  = re.compile(r" F\d*\.?\d*")
                extrude_re = re.compile(r" E-?\d*\.?\d*")
                params_re = re.compile(r"([YEF]-?\d*\.?\d+)")

                for i, layer in enumerate(gcode_list):
                    if i < 2 or i > len(gcode_list) - 1:
                        continue
                    lines = layer.splitlines()
                    for ln, line in enumerate(lines):
                        has_e = has_axis = False
                        cmd = line.split(' ', 1)[0]
                        if cmd not in ["G0", "G1", "G92"]:
                            continue
                        matches = re.findall(params_re, line)
                        if not matches:
                            continue
                        for m in matches:
                            p, v = m[:1], float(m[1:])
                            if p == "Y":   y = v; has_axis = True
                            elif p == "E": e = v; has_e = True
                            elif p == "F": f = v
                            elif p in "XZ": has_axis = True
                        if (cmd != "G92" and has_axis and has_e and f is not None
                                and y is not None and y <= minimum_y
                                and last_y is not None and last_y <= minimum_y):
                            if f > wall_speed:
                                line = re.sub(speed_re, "", line)
                            if wall_flow != 1.0 and last_e is not None:
                                new_e = last_e + (e - last_e) * wall_flow
                                line = re.sub(extrude_re, " E%f" % new_e, line)
                                line += " ; Adjusted E\nG92 E%f" % e
                            if f > wall_speed:
                                g_type = int(line[1:2])
                                line = "G%d F%d\n%s\nG%d F%d" % (g_type, wall_speed, line, g_type, f)
                            lines[ln] = line
                        last_y = y; last_e = e
                    gcode_list[i] = "\n".join(lines) + "\n"

            # Remove finalize bits before end gcode
            gcode_list[-1] = gcode_list[-1].replace("M140 S0\nM203 Z5\nM107", "")

            # Repetitions
            if repetitions > 1 and len(gcode_list) > 2:
                rep_gcode = repetitions_gcode.replace("{belt_repetitions_distance}", str(rep_distance))
                layers = gcode_list[2:-1]
                layers.append(rep_gcode)
                gcode_list[2:-1] = (layers * int(repetitions))[0:-1]

            gcode_list[0] += ";BELTPROCESSED\n"
            gcode_dict[plate_id] = gcode_list
            dict_changed = True

        if dict_changed:
            setattr(scene, "gcode_dict", gcode_dict)

    @pyqtSlot()
    def resetSlice(self):
        self._application.getBackend().backendStateChange.emit(BackendState.NotStarted)
