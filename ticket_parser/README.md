# Markdown Ticket Parser

Codebase summary: A Python application that parses markdown ticket files into JSON format using `markdown_it`.

## Features

- Parse structured markdown tickets with name, description, and tasks
- Validate markdown format strictly (only allows specific structure)
- Convert to JSON with schema validation
- Command-line interface for easy usage

## Installation

```bash
pip install -r requirements.txt
```

## Ticket Format

Markdown tickets must follow this exact format:

```markdown
# Ticket Name

## Description
A description of the ticket in plaintext. Multiple paragraphs are allowed here.

## Tasks
- First task as a bullet point.
- Second task as a bullet point.
- Only bullet points are allowed in this section.
```

### Rules

- **One H1 heading** (`#`) for the ticket name
- **Two H2 headings** (`##`): `Description` and `Tasks`
- **Description section**: Only paragraphs of text allowed
- **Tasks section**: Only bullet points allowed
- No other content or sections permitted

## Usage

### As a Library

```python
from src.ticket_parser import TicketParser
from jsonschema import validate
from src.schema import TICKET_SCHEMA

parser = TicketParser()

# Parse from file
ticket = parser.parse_file('samples/ticket1.md')

# Parse from string
markdown_content = """
# Fix Authentication Bug

## Description
Users cannot login.

## Tasks
- Investigate issue.
- Fix bug.
"""
ticket = parser.parse(markdown_content)

# Validate against schema
validate(instance=ticket, schema=TICKET_SCHEMA)

print(ticket)
# Output:
# {
#     'name': 'Fix Authentication Bug',
#     'description': 'Users cannot login.',
#     'tasks': ['Investigate issue.', 'Fix bug.']
# }
```

### Command Line

```bash
# Parse a ticket file
python src/cli.py samples/ticket1.md

# Validate and save to file
python src/cli.py samples/ticket1.md --validate -o output.json
```

## Examples

See the `samples/` directory for example ticket files:
- `ticket1.md` - Bug fix ticket
- `ticket2.md` - Feature implementation ticket
- `ticket3.md` - Performance optimization ticket

## Testing

Run tests with pytest:

```bash
python3 -m pytest tests/ -v
```

The test suite includes:
- Basic parsing functionality tests
- Error handling tests
- JSON schema validation tests
- Tests for all sample files
