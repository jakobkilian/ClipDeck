from ableton.v2.control_surface import ControlSurface

def create_instance(c_instance: ControlSurface) -> ControlSurface:
    from .clipdeck_cs import ClipDeck
    return ClipDeck(c_instance)
