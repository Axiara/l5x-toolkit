"""
L5X Agent Toolkit - AI-driven manipulation of Rockwell Automation L5X files.

This toolkit provides validated, atomic operations for reading and modifying
Studio 5000 Logix Designer L5X project files. It is designed to be invoked
by an AI agent (via MCP or direct function calls) to ensure hyper-accurate
L5X file manipulation.

Core Design Principle:
    The AI agent never touches raw XML. Instead, it invokes validated tool
    functions that produce structurally correct XML every time.

Usage:
    from l5x_agent_toolkit import L5XProject

    # Load a project
    project = L5XProject('path/to/project.L5X')

    # Query operations
    programs = project.list_programs()
    tags = project.list_controller_tags()
    summary = project.get_project_summary()

    # Tag operations
    from l5x_agent_toolkit import tags
    tags.create_tag(project, 'MyTag', 'DINT', description='A counter')
    tags.set_tag_value(project, 'MyTag', 42)

    # Program/routine operations
    from l5x_agent_toolkit import programs
    programs.create_program(project, 'ConveyorControl', description='Conveyor logic')
    programs.add_rung(project, 'ConveyorControl', 'MainRoutine',
                      'XIC(StartPB)OTE(MotorRun);', comment='Start motor')

    # Rung operations
    from l5x_agent_toolkit import rungs
    errors = rungs.validate_rung_syntax('XIC(tag1)OTE(tag2);')
    new_text = rungs.substitute_tags('XIC(OldTag)OTE(OldOut);',
                                      {'OldTag': 'NewTag', 'OldOut': 'NewOut'})

    # Import operations
    from l5x_agent_toolkit import aoi, udt, modules
    aoi.import_aoi(project, 'path/to/MyAOI.L5X')
    udt.import_udt(project, 'path/to/MyUDT.L5X')
    modules.import_module(project, 'path/to/module_template.L5X',
                          name='VFD01', address='192.168.1.100')

    # Validation
    from l5x_agent_toolkit import validator
    result = validator.validate_project(project)
    if not result.is_valid:
        print(result)

    # Write the modified project
    project.write('path/to/output.L5X')
"""

__version__ = '0.2.0'


def __getattr__(name):
    """Lazy import to avoid circular/missing module errors during development."""
    if name == 'L5XProject':
        from .project import L5XProject
        return L5XProject
    if name in ('L5XPlugin', 'PluginContext'):
        from .plugin import L5XPlugin, PluginContext
        return L5XPlugin if name == 'L5XPlugin' else PluginContext
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    'L5XProject',
    'L5XPlugin',
    'PluginContext',
]
