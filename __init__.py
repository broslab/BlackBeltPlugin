# Copyright (c) 2018 fieldOfView
# The Blackbelt plugin is released under the terms of the LGPLv3 or higher.
# Adapted for Cura 4.10 by broslab and ClaudeCode

from . import BeltPlugin

def getMetaData():
    return {}

def register(app):
    return {"extension": BeltPlugin.BeltPlugin()}
