"""
L5X Schema Constants and Validation Rules.

Defines element ordering, data types, instruction catalog, and structural
requirements for valid L5X files.
"""

# Required child elements of <Controller>, in order.
# Studio 5000 expects this exact sequence.
CONTROLLER_CHILD_ORDER = [
    'RedundancyInfo',
    'Security',
    'SafetyInfo',
    'DataTypes',
    'Modules',
    'AddOnInstructionDefinitions',
    'AlarmDefinitions',
    'Tags',
    'Programs',
    'Tasks',
    'CST',
    'WallClockTime',
    'Trends',
    'DataLogs',
    'TimeSynchronize',
    'EthernetPorts',
    'OpcUaInfo',
]

# Required child elements of <Tag>, in order.
# Studio 5000 rejects imports when these are out of sequence.
TAG_CHILD_ORDER = [
    'AlarmConditions',
    'ConsumeInfo',
    'Description',
    'Data',
    'ForceData',
    'Comments',
]

# Base (atomic) data types supported by Logix 5000
BASE_DATA_TYPES = {
    'BOOL':  {'size_bits': 1,  'radix': 'Decimal', 'default': '0'},
    'SINT':  {'size_bits': 8,  'radix': 'Decimal', 'default': '0'},
    'USINT': {'size_bits': 8,  'radix': 'Decimal', 'default': '0'},
    'INT':   {'size_bits': 16, 'radix': 'Decimal', 'default': '0'},
    'UINT':  {'size_bits': 16, 'radix': 'Decimal', 'default': '0'},
    'DINT':  {'size_bits': 32, 'radix': 'Decimal', 'default': '0'},
    'UDINT': {'size_bits': 32, 'radix': 'Decimal', 'default': '0'},
    'LINT':  {'size_bits': 64, 'radix': 'Decimal', 'default': '0'},
    'REAL':  {'size_bits': 32, 'radix': 'Float',   'default': '0.0'},
    'LREAL': {'size_bits': 64, 'radix': 'Float',   'default': '0.0'},
    'STRING': {'size_bits': 0, 'radix': 'ASCII',   'default': "''"},
}

# Built-in structure types with their members
BUILTIN_STRUCTURES = {
    'TIMER': {
        'members': [
            ('PRE', 'DINT', 'Decimal'),
            ('ACC', 'DINT', 'Decimal'),
            ('EN',  'BOOL', 'Decimal'),
            ('TT',  'BOOL', 'Decimal'),
            ('DN',  'BOOL', 'Decimal'),
        ],
        'l5k_default': '[0,0,0]',
    },
    'COUNTER': {
        'members': [
            ('PRE', 'DINT', 'Decimal'),
            ('ACC', 'DINT', 'Decimal'),
            ('CU',  'BOOL', 'Decimal'),
            ('CD',  'BOOL', 'Decimal'),
            ('DN',  'BOOL', 'Decimal'),
            ('OV',  'BOOL', 'Decimal'),
            ('UN',  'BOOL', 'Decimal'),
        ],
        'l5k_default': '[0,0,0]',
    },
    'CONTROL': {
        'members': [
            ('LEN', 'DINT', 'Decimal'),
            ('POS', 'DINT', 'Decimal'),
            ('EN',  'BOOL', 'Decimal'),
            ('EU',  'BOOL', 'Decimal'),
            ('DN',  'BOOL', 'Decimal'),
            ('EM',  'BOOL', 'Decimal'),
            ('ER',  'BOOL', 'Decimal'),
            ('UL',  'BOOL', 'Decimal'),
            ('IN',  'BOOL', 'Decimal'),
            ('FD',  'BOOL', 'Decimal'),
        ],
        'l5k_default': '[0,0,0]',
    },
}

# Valid radix values for data display
VALID_RADIX = {
    'Decimal', 'Float', 'Binary', 'Octal', 'Hex', 'ASCII',
    'NullType',   # used for nested structures
    'Exponential',
}

# Valid ExternalAccess values
VALID_EXTERNAL_ACCESS = {'Read/Write', 'Read Only', 'None'}

# Valid OpcUaAccess values
VALID_OPC_UA_ACCESS = {'None', 'ReadOnly', 'ReadWrite'}

# Valid task types
VALID_TASK_TYPES = {'CONTINUOUS', 'PERIODIC', 'EVENT'}

# Valid routine types
VALID_ROUTINE_TYPES = {'RLL', 'ST', 'FBD', 'SFC'}

# Valid parameter usage for AOIs
VALID_PARAMETER_USAGE = {'Input', 'Output', 'InOut'}

# Valid rung types
VALID_RUNG_TYPES = {'N', 'D', 'S'}  # Normal, Diagnostic, Safety

# Maximum limits
MAX_PROGRAMS_PER_TASK = 1000
MAX_TAG_NAME_LENGTH = 40
MAX_DESCRIPTION_LENGTH = 512

# Valid characters in tag names (letters, digits, underscore; must start with letter or underscore)
TAG_NAME_PATTERN = r'^[A-Za-z_][A-Za-z0-9_]*$'

# Common RLL instructions with their parameter counts
# Format: 'INSTRUCTION': (min_params, max_params)
INSTRUCTION_CATALOG = {
    # Input instructions
    'XIC': (1, 1),
    'XIO': (1, 1),
    'ONS': (1, 1),
    'OSR': (3, 3),
    'OSF': (3, 3),

    # Output instructions
    'OTE': (1, 1),
    'OTL': (1, 1),
    'OTU': (1, 1),

    # Timer/Counter
    'TON': (3, 3),
    'TOF': (3, 3),
    'RTO': (3, 3),
    'CTU': (3, 3),
    'CTD': (3, 3),
    'RES': (1, 1),

    # Compare
    'EQU': (2, 2),
    'NEQ': (2, 2),
    'LES': (2, 2),
    'LEQ': (2, 2),
    'GRT': (2, 2),
    'GEQ': (2, 2),
    'CMP': (1, 1),
    'LIM': (3, 3),
    'MEQ': (3, 3),

    # Math
    'ADD': (3, 3),
    'SUB': (3, 3),
    'MUL': (3, 3),
    'DIV': (3, 3),
    'MOD': (3, 3),
    'NEG': (2, 2),
    'ABS': (2, 2),
    'SQR': (2, 2),
    'CPT': (2, 2),

    # Move/Logic
    'MOV': (2, 2),
    'MVM': (3, 3),
    'BTD': (5, 5),
    'CLR': (1, 1),
    'AND': (3, 3),
    'OR':  (3, 3),
    'XOR': (3, 3),
    'NOT': (2, 2),
    'BAND': (1, 9),
    'BOR':  (1, 9),
    'BXOR': (1, 9),
    'BNOT': (2, 2),

    # Array/File
    'COP': (3, 3),
    'FLL': (3, 3),
    'AVE': (5, 5),
    'SRT': (3, 3),
    'STD': (5, 5),
    'SIZE': (2, 2),

    # Program control
    'JSR': (1, None),  # Variable params: routine name + optional params
    'RET': (0, None),
    'JMP': (1, 1),
    'LBL': (1, 1),
    'NOP': (0, 0),
    'AFI': (0, 0),
    'EOT': (0, 0),
    'SBR': (0, None),
    'TND': (0, 0),
    'MCR': (0, 0),
    'FOR': (3, 3),
    'BRK': (0, 0),

    # Conversion
    'TOD': (2, 2),
    'FRD': (2, 2),
    'TRN': (2, 2),
    'DTOS': (2, 2),
    'STOD': (2, 2),
    'UPPER': (2, 2),
    'LOWER': (2, 2),

    # String
    'CONCAT': (3, 3),
    'MID': (4, 4),
    'DELETE': (4, 4),
    'INSERT': (4, 4),
    'FIND': (3, 3),

    # Special
    'MSG': (1, 1),
    'GSV': (4, 4),
    'SSV': (4, 4),
    'EVENT': (1, 1),
    'IOT': (1, 1),
    'UID': (0, 0),
    'UIE': (0, 0),

    # Alarm
    'ALMD': (1, 1),
    'ALMA': (1, 1),
}

# Instructions that can appear as output-only (right side of rung)
OUTPUT_INSTRUCTIONS = {
    'OTE', 'OTL', 'OTU', 'TON', 'TOF', 'RTO', 'CTU', 'CTD', 'RES',
    'MOV', 'MVM', 'ADD', 'SUB', 'MUL', 'DIV', 'MOD', 'NEG', 'ABS',
    'SQR', 'CPT', 'CLR', 'COP', 'FLL', 'AVE', 'SRT', 'STD', 'SIZE',
    'JSR', 'RET', 'JMP', 'NOP', 'AFI', 'EOT', 'TND', 'MCR',
    'MSG', 'GSV', 'SSV', 'BTD', 'AND', 'OR', 'XOR', 'NOT',
    'TOD', 'FRD', 'TRN', 'DTOS', 'STOD',
    'CONCAT', 'MID', 'DELETE', 'INSERT', 'FIND',
    'UPPER', 'LOWER', 'EVENT', 'IOT',
    'ALMD', 'ALMA',
    'FOR', 'BRK',
    'BAND', 'BOR', 'BXOR', 'BNOT',
}

# ---------------------------------------------------------------------------
# Alarm constants
# ---------------------------------------------------------------------------

# Valid alarm condition types for tag-based alarms
VALID_ALARM_CONDITION_TYPES = {
    'TRIP', 'HIHI', 'HI', 'LO', 'LOLO',
    'DEV_HI', 'DEV_LO', 'ROC_POS', 'ROC_NEG',
}

ALARM_SEVERITY_MIN = 1
ALARM_SEVERITY_MAX = 1000

# Default attributes for AlarmDigitalParameters on ALARM_DIGITAL tags
ALARM_DIGITAL_DEFAULTS = {
    'Severity': '500',
    'MinDurationPRE': '0',
    'ShelveDuration': '0',
    'MaxShelveDuration': '0',
    'ProgTime': 'DT#1970-01-01-00:00:00.000_000Z',
    'EnableIn': 'false',
    'In': 'false',
    'InFault': 'false',
    'Condition': 'true',
    'AckRequired': 'true',
    'Latched': 'false',
    'ProgAck': 'false',
    'OperAck': 'false',
    'ProgReset': 'false',
    'OperReset': 'false',
    'ProgSuppress': 'false',
    'OperSuppress': 'false',
    'ProgUnsuppress': 'false',
    'OperUnsuppress': 'false',
    'OperShelve': 'false',
    'ProgUnshelve': 'false',
    'OperUnshelve': 'false',
    'ProgDisable': 'false',
    'OperDisable': 'false',
    'ProgEnable': 'false',
    'OperEnable': 'false',
    'AlarmCountReset': 'false',
    'UseProgTime': 'false',
}

# Default attributes for MemberAlarmDefinition
MEMBER_ALARM_DEFINITION_DEFAULTS = {
    'Limit': '0.0',
    'Severity': '500',
    'OnDelay': '0',
    'OffDelay': '0',
    'ShelveDuration': '0',
    'MaxShelveDuration': '0',
    'Deadband': '0.0',
    'Required': 'false',
    'AlarmSetOperIncluded': 'true',
    'AlarmSetRollupIncluded': 'true',
    'AckRequired': 'false',
    'Latched': 'false',
    'EvaluationPeriod': '500 millisecond',
    'Expression': '= 1',
}


# Default ExportOptions for generated L5X files
DEFAULT_EXPORT_OPTIONS = "NoRawData L5KData DecoratedData ForceProtectedEncoding AllProjDocTrans"

# Export date format matching Studio 5000 output (e.g. "Thu Feb 12 10:00:00 2026")
EXPORT_DATE_FORMAT = "%a %b %d %H:%M:%S %Y"
