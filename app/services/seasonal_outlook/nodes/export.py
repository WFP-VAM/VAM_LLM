"""Node export: the report's Word files and ZIP, stored as the phase's artifacts."""


def export(state, build):
    """build(state) makes and stores the files; the runner binds it to the analysis and its store."""
    return {'artifacts': build(state)}
