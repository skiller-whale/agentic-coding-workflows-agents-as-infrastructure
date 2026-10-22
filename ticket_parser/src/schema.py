"""JSON schema for ticket validation."""

TICKET_SCHEMA = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "type": "object",
    "properties": {
        "name": {
            "type": "string",
            "minLength": 1,
            "description": "The ticket name from H1 heading"
        },
        "description": {
            "type": "string",
            "minLength": 1,
            "description": "The ticket description from Description section"
        },
        "tasks": {
            "type": "array",
            "items": {
                "type": "string",
                "minLength": 1
            },
            "minItems": 1,
            "description": "List of tasks from Tasks section"
        }
    },
    "required": ["name", "description", "tasks"],
    "additionalProperties": False
}
